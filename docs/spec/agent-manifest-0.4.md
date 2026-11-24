# Agent manifest spec 0.4

Status: draft. Adds three things to [0.3](agent-manifest-0.3.md). **Actions** (§2 to §7): what an
agent may ask to have done besides answer, and how the runtime gates it. **A calibrated
confidence and a second signal** (§8 to §13): an `agent` variant's `calibration`, `repeat` on
an `agent` or an `llm` variant, and the composite type `check`, each of which can only lower a
decision's automation. **Written output** (§16 to §20): what an agent writes, the rubrics it
is graded against, and when a model judge's grades are believed. Every other part of an agent
is as [0.2](agent-manifest-0.2.md) and 0.3 say, and a 0.4 manifest that uses none of this is a
0.3 manifest with `spec_version: "0.4"`. Every valid 0.1, 0.2 and 0.3 agent is still valid,
with unchanged behaviour (§14). The key words MUST, SHOULD and MAY are used as in RFC 2119.

The reference implementation is `mendari.core.spec` (the manifest), `mendari.core.policy` (what
an effect needs, and the order of the caps), `mendari.core.actions` (checking a proposal),
`mendari.providers.agent_calibration`, `mendari.providers.repeat`, `mendari.providers.check`,
`mendari.providers.judge` (the judge) and `mendari.evals.written` (the run, the ledger and the
report). The decisions behind it are
[ADR-0023](../decisions/ADR-0023-an-agent-proposes-actions.md) (actions),
[ADR-0024](../decisions/ADR-0024-a-checked-confidence-and-a-second-signal.md) (the calibration
and the second signal) and
[ADR-0025](../decisions/ADR-0025-a-judge-is-trusted-by-measured-agreement.md) (written
output).

## 1. What changes

An agent's answers say what it concluded. Its **actions** are what it asks to have done: a
message sent, a note filed, an order placed. 0.4 adds:

- the manifest section `actions` (§2): each action's name, description, `effect` and,
  optionally, a schema for its arguments;
- what each effect needs before an action is released (§3), which the runtime sets and a
  manifest may only tighten;
- two keys in the agent contract (§4): the declared actions in each request, the proposed
  actions in each response;
- `actions` in a decision's result, each with a disposition (§5), and one more cap in the
  policy;
- how a review releases or denies held actions, and how a caller asks again (§6);
- two audit events (§7);
- three keys of a provider variant (§8 to §11): `calibration` on an `agent` variant, `repeat`
  on an `agent` or an `llm` variant, and the composite type `check`. Each can find a reason
  that a decision may not be automated, and none can make one automated;
- the order in which the policy applies every cap, the new ones last (§12).

The runtime authorizes actions. It runs none: the caller runs an action, and only one that was
released.

## 2. The `actions` section

```yaml
actions:
  <name>:
    description: What it does, in words a reviewer reads   # 1 to 300 characters
    effect: read | draft | write | contact | order        # §2.1
    allow_auto: false      # optional: never released without a person (§3)
    schema: {...}          # optional: a JSON Schema for its arguments (§2.2)
```

Validation rules (all MUST hold):

- A name matches `^[a-z][a-z0-9_]*$` and has at most 64 characters. An agent declares at most
  16 actions.
- `description` is 1 to 300 characters, and not blank. It is documentation and the agent's
  instructions: it is shown on the Decision Card and sent to the agent as written.
- `effect` is one of the five in §2.1.
- `schema`, if given, is held to §2.2.
- An action takes no other key. No key names a gate.
- The declared actions as one request sends them (§4.1) are at most 32,000 characters.
- An agent that declares any action MUST declare `spec_version: "0.4"` (§14).

### 2.1 Effects

| `effect` | The action |
|---|---|
| `read` | reads data and changes nothing |
| `draft` | prepares something a person sees before it takes effect |
| `write` | changes the chart or another system of record |
| `contact` | sends something to a patient, or to anyone outside the care team |
| `order` | places an order or a prescription, or changes treatment |

An action that fits two takes the later one in this table. The effect is the author's
declaration, as the risk level is: a runtime cannot check what a tool does, so whoever deploys
an agent reviews its effects on the Decision Card.

### 2.2 The arguments schema

`schema` is a JSON Schema 2020-12 document, written inline, that uses only these keywords:
`type`, `properties`, `required`, `additionalProperties`, `items`, `enum`, `const`,
`minLength`, `maxLength`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`,
`minItems`, `maxItems`, `title`, `description`. And:

- every schema in it has a `type`, one name: `object`, `array`, `string`, `integer`, `number`
  or `boolean`;
- every object sets `additionalProperties: false`;
- an object has at most 32 properties, named `^[A-Za-z_][A-Za-z0-9_]{0,63}$`, and `required`
  names only its properties;
- an `enum` lists 1 to 100 values, none an object or a list, texts of at most 200 characters; a
  `const` is no object or list;
- objects nest at most 4 deep, and the schema is at most 4,000 characters as JSON.

It has no `pattern`, `format`, `$ref` or combinator: an action's arguments are written by a
model, and checking them must cost no more than reading them. Any other keyword makes the agent
invalid, named by where it is and the keyword. Without a `schema`, the arguments are any JSON
object within §4.2's limits; an action whose effect is `write`, `contact` or `order` SHOULD
have one.

### 2.3 Example

```yaml
spec_version: "0.4"
# ...
actions:
  look_up_medications:
    description: Read the patient's active medication list.
    effect: read
  draft_reply:
    description: Draft a reply for staff to read and send themselves.
    effect: draft
    schema:
      type: object
      additionalProperties: false
      required: [message]
      properties:
        message: {type: string, minLength: 1, maxLength: 2000}
  send_reply:
    description: Send a reply to the patient through the portal.
    effect: contact
    schema:
      type: object
      additionalProperties: false
      required: [message]
      properties:
        message: {type: string, minLength: 1, maxLength: 2000}
  queue_refill:
    description: Put a refill request in the pharmacy queue for a prescriber to sign.
    effect: write
    allow_auto: false
    schema:
      type: object
      additionalProperties: false
      required: [medication]
      properties:
        medication: {type: string, minLength: 1, maxLength: 200}
        days_supply: {type: integer, minimum: 1, maximum: 90}
  renew_prescription:
    description: Renew the prescription as it was last written.
    effect: order
```

## 3. What an effect needs

The runtime sets this, and an agent cannot change it but by tightening
([ADR-0002](../decisions/ADR-0002-runtime-enforced-risk-caps.md), ADR-0023). Each declared
action has a **gate**:

| Gate | Effects | Released |
|---|---|---|
| `decision` | `read`, `draft` | with any decision that is not `BLOCK` |
| `auto` | `write`, `contact` | with an `AUTO` decision; otherwise when the decision's reviewer confirms it |
| `person` | `order` | never without a person: when the decision's reviewer confirms it |

- `allow_auto: false` gives an action the gate `person`, whatever its effect. No key loosens a
  gate: `allow_auto: true` is the default and changes nothing, on an `order` too.
- For an agent the deployment has not vouched for (a community agent, or one from a registry
  before the organization approves its digest, or while its registry recalls it), `read` and
  `draft` take the gate `auto`: nothing it proposes is released without a person.
- A runtime MUST NOT release an action a stricter reading of this section would not release. It
  MAY be stricter.

Since a decision is `AUTO` only where the agent's risk level allows it (0.2 §7), so are `write`
and `contact` released without a person only there: at levels 0 and 1; at levels 2 and 3 with
the deploying organization's recorded approval, which covers the agent's actions as it covers
its answers; at level 4 never.

## 4. The agent contract

The contract of an `agent` provider (0.2 §5) gains one key in each direction, for a 0.4 agent
only.

### 4.1 The request

A request to an agent whose manifest declares actions carries them, in the manifest's order:

```json
"actions": [
  {"name": "draft_reply", "description": "Draft a reply for staff to read and send themselves.",
   "effect": "draft", "schema": {"type": "object", "additionalProperties": false,
   "required": ["message"], "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 2000}}}},
  {"name": "renew_prescription", "description": "Renew the prescription as it was last written.",
   "effect": "order"}
]
```

`schema` is present when the manifest gives one. `allow_auto` is not sent: the gate is not the
agent's to read. An agent with no declared action gets no `actions` key, and its requests are
what they were.

### 4.2 The response

The agent MAY propose actions with its answers:

```json
"actions": [
  {"name": "draft_reply", "arguments": {"message": "Your refill request is with the pharmacy."}},
  {"name": "queue_refill", "arguments": {"medication": "lisinopril 10 mg", "days_supply": 30}}
]
```

- At most 16. Each is an object with `name` and, optionally, `arguments` (an object; `{}` when
  left out), and no other key.
- `name` has the shape of an action's name (§2).
- `arguments` is at most 16,000 characters as JSON, nests at most 8 deep, and holds only what
  JSON carries.
- Two proposals with the same name and the same arguments are one.

A response that breaks any of these is not one the contract allows: the provider error
`invalid_response`, and the slot's questions fail, as for any malformed answer. A proposal that
is well formed but names an action the manifest does not declare, or whose arguments do not
match its schema, is not an error: that action is blocked and the decision is not automated
(§5).

Proposing is asking. An agent MUST NOT run an action it proposes whose effect is `write`,
`contact` or `order`. `tool_calls` (0.2) is unchanged: what the agent reports having done to
reach its answers, kept as evidence.

A member of an `any_of`, `cascade` or `vote` composite MAY be an agent, but its proposals are
not taken in this version: each is blocked (`composite_member`), and the decision is not
automated. A `check` (§11) is the one composite whose proposals are taken: its first member
answers, so what that member proposes is what the slot proposes.

### 4.3 What a 0.1 to 0.3 agent's response may hold

An `actions` key in the response of an agent on spec 0.1, 0.2 or 0.3 is ignored, as any key the
contract does not name always was. Nothing is proposed, released or recorded.

## 5. The decision's result

A decision of a 0.4 agent that declares actions, or whose agent proposed any, has `actions`: one
entry per proposal, in the order proposed (an empty list when none was).

```json
{"id": "0f6c2c1e-…", "name": "queue_refill", "effect": "write",
 "disposition": "held", "reason": "awaiting_review",
 "arguments": {"medication": "lisinopril 10 mg", "days_supply": 30},
 "arguments_hash": "hmac-sha256:9f1c…:5b0e…"}
```

`disposition` is `released` (the caller may run it), `held` (it waits for the decision's
reviewer) or `blocked` (it MUST NOT be run). The caller runs released actions only. An
undeclared action has `effect: null`. An action whose arguments do not match its schema also
has `problems`: where, and which rule of the schema, never a value. `arguments_hash` is hashed
as the input hash is (0.2 §12): keyed where the deployment has an input hash key.

**Evaluation order.** 0.2 §6 gains, in step 5, one more cap that only lowers `AUTO` to
`HUMAN_REVIEW`: a proposed action that could not be released with an `AUTO` decision. That is
an undeclared action, arguments that do not match their schema, a composite member's proposal,
or an action whose gate is `person`. The reasons say which, naming only declared actions, each
followed by `; a person decides`. The recommendation and the priority do not change, a decision
already going to a person keeps its outcome (and gets the reason), and a `BLOCK` stands and
says nothing more. It is the last cap applied (§12). Then each proposal's disposition follows
from the outcome:

| The decision | gate `decision` | gate `auto` | gate `person` |
|---|---|---|---|
| `BLOCK` | blocked `decision_blocked` | blocked `decision_blocked` | blocked `decision_blocked` |
| no recommendation (`ABSTAIN`, or a review without one) | released `with_decision` | blocked `no_recommendation` | blocked `no_recommendation` |
| `AUTO` | released `with_decision` | released `automated` | (the cap above made the decision `HUMAN_REVIEW`) |
| `HUMAN_REVIEW` with a recommendation | released `with_decision` | held `awaiting_review` | held `awaiting_review` |

An undeclared action is blocked `undeclared`; arguments that do not match, `invalid_arguments`;
a composite member's, `composite_member`. An action is held only on a decision that awaits
review and has a recommendation: only there is there someone to wait for.

A proposal that a second signal did not agree on (§10, §11) has the gate `person`, whatever its
effect: it is held for the decision's reviewer where the table holds, blocked where it blocks,
and never released without a person.

## 6. Review, and asking again

**Review.** When a reviewer resolves a decision (0.2 §12), its held actions are resolved with
it: `released` (`confirmed`) when the reviewer's label is the recommendation, `denied`
(`overridden`) when it is another. A reviewer MAY deny any held action and still confirm the
label (`reviewer_denied`): the reference server takes their ids as `deny_actions`, and the
review is then no override. A resolved action does not change again: a release is final.

**Asking again.** A caller MAY ask, for a decision already recorded, whether one action with
these arguments may run. The reference server exposes it as
`POST /api/agents/{namespace}/{name}/actions` (`{decision_id, name, arguments}`), and
`mendari mcp` as the tool `authorize_action`. A blocked action is not an error of the call.

- An action already proposed on that decision (the same declared name, the same arguments)
  answers as it stands now, and nothing new is recorded. So a caller holding a held action
  asks again to learn that it was released, and a caller about to run a released action asks
  to check that these are the arguments that were released.
- A new action is checked and gated as §5 says, by the decision as it stands: on an automated
  decision a `person` action is blocked (`no_reviewer`: nobody is waiting), and an `auto`
  action is blocked while the agent may not automate now (`not_automated`: a hold, an approval
  no longer in force, a recall) or after an earlier proposal on that decision was refused
  (`earlier_refusal`); on a decision already reviewed, `auto` and `person` actions are blocked
  (`review_closed`); on a decision made by another version of the agent, everything is
  (`agent_changed`).
- There is no authorization without a decision. An agent behind the contract proposes with its
  answer.

## 7. Audit records

Each proposal is one event on its decision, `action.proposed`, chained after
`decision.created` in the same transaction (or when a caller asks, §6): the action's id, its
name and effect when the manifest declares it (else null), the disposition, the reason, the
arguments' hash, whether it came with the answer or was asked for, and who asked. Each held
action's resolution is one `action.resolved`: its id, name, effect, disposition, reason and the
reviewer.

```json
{"action_id": "b2…", "name": "queue_refill", "effect": "write", "disposition": "held",
 "reason": "awaiting_review", "arguments_hash": "hmac-sha256:9f1c…:5b0e…",
 "source": "answer", "slot": "reader", "note_ref": {"id": "…", "sha256": "…"}, "org": "default"}
```

```json
{"action_id": "b2…", "name": "queue_refill", "effect": "write", "disposition": "released",
 "reason": "confirmed", "reviewer": "rn-1", "org": "default"}
```

An event never holds the arguments, nor a name the manifest does not declare: both are the
agent's text and can hold patient data. A runtime MAY keep them apart from the chain, and MUST
then keep them as it keeps the decision's input. The reference runtime keeps them as the
event's note (`note_ref` is its commitment): sealed under PHI mode `encrypt`, in the clear
apart from the chain under `off`, not at all under `hash` or for an agent whose
`audit.store_input` is `hash`; read as inputs are (each read chained), erased with the
decision's kept input, and removed by retention.

## 8. A calibrated confidence and a second signal: what a cap is

Three keys of a provider variant, and nothing else of the manifest: `calibration` on an
`agent` variant (§9), `repeat` on an `agent` or an `llm` variant (§10), and the composite type
`check` (§11). A manifest that uses any of them MUST say `spec_version: "0.4"`; an engine MUST
refuse it otherwise, naming each place it is used:

```text
['providers.reader.variants.agent.repeat'] use spec 0.4 features; set spec_version: '0.4'
```

Each of the three can find a reason that a decision may not be automated. Such a reason is a
**cap**. After the policy of 0.2 §6 has decided, a runtime MUST apply the caps the
decision's providers found (§12 gives the order):

- a decision that is `AUTO` becomes `HUMAN_REVIEW`, and each cap is added to its reasons,
  followed by `; a person decides`;
- a decision that already goes to a person (`HUMAN_REVIEW`, `ABSTAIN`) keeps its outcome,
  recommendation, priority and rule, and gets each cap the same way;
- a `BLOCK` keeps them too, and each cap is added to its reasons as it is: a block is final,
  and no person decides.

A cap MUST NOT be lifted by a governance override, by the risk level, or by any key of the
manifest, and a runtime MUST NOT offer a manifest key that disables one. Nothing in §8 to §11
can make a decision `AUTO` that the policy alone would not have.

A **deciding question** is the primary question, a question named in `evaluation.critical`, a
question a policy rule's `when` reads, or a question any of those is derived from. A
disagreement (§10 and §11) is a cap only on a deciding question; on any other question it
is written in the answer's evidence.

## 9. `calibration` on an `agent` variant

```yaml
providers:
  reader:
    answers: [request, urgent_symptoms]
    use: agent
    variants:
      agent:
        type: agent
        agent_id: portal-agent
        state_fields: [message.subject, message.body]
        calibration: models/reader-agent-calibration.json
```

`calibration` names a file of the agent, so the agent's digest covers it. The file is
`mendari.agent-calibration/1`:

```json
{
  "format": "mendari.agent-calibration/1",
  "agent": {"id": "portal-agent", "version": "2.1.0", "model": null},
  "method": "temperature",
  "temperatures": {"request": 1.66},
  "questions": {"request": {"cases": 400, "nll_before": 0.7571, "nll_after": 0.6703,
                            "ece_before": 0.1246, "ece_after": 0.0269}},
  "not_fitted": {"urgent_symptoms": "its 400 answers place no temperature: ..."},
  "fitted_on": {"dataset": "calibration", "sha256": "…", "cases": 400, "date": "2026-11-24"},
  "failures": {},
  "cost_usd": 0.4
}
```

A reader of the file MUST find `format`, `agent.version` (a string), `temperatures` (each a
positive finite number, by question) and `fitted_on` with `dataset`, `cases` and `date`. A file
without them is not a calibration, and a variant that names it MUST NOT run. The other keys are
a record of the fit.

**The map.** For a question with a temperature T, the distribution p the provider would have
given becomes p'(label) proportional to p(label)^(1/T), and the answer's confidence is p' of
the answer's own label. When the agent's response gave only a confidence c for its answer over
K labels, p is c for that label and (1 - c) / (K - 1) for each other label (0.2 §5), so
the map scales log(c (K - 1) / (1 - c)) by 1 / T. The answer's label MUST be kept as the agent
gave it; the map keeps the order of the labels, so no other label has a larger p'. A question
with no temperature is left as reported.

**The version.** A runtime MUST compare, for each call, the variant's `agent_id` and the
`agent_version` in the agent's response with `agent.id` and `agent.version` of the file.

- Equal: the map is applied.
- Not equal, or the response has no `agent_version`: the map MUST NOT be applied, and the
  decision carries the cap
  `the agent changed since its confidence was calibrated (was X, is Y)`, where X is the file's
  version and Y the reported one, or `not reported`. When the two `agent_id`s differ, X and Y
  each give the id and then the version.

Answers read from the variant's `mock_file` are not the agent's confidence: the map MUST NOT be
applied to them, and they carry no version cap.

**The fit** (`mendari calibrate <agent> --slot S --variant V`) asks the live agent about each
case of a labelled dataset once and fits each question's T by minimising the negative
log-likelihood of the true label. A fitter:

- MUST NOT fit on canned answers, on a service that reports no `agent_version`, or on one that
  reported more than one version while it was asked;
- MUST NOT fit a question on fewer than 100 labelled cases the agent answered, and MUST leave a
  question unfitted when no temperature is the best one (the best is at the edge of its search);
- MUST record the dataset's name, its sha256, the number of cases and the date, and the
  `agent_id` and reported `agent_version`;
- SHOULD be given a dataset that no evaluation reports on (an agent's `calibration` set), and
  SHOULD say so when it is given one of the agent's evaluation sets.

## 10. `repeat` on an `agent` or an `llm` variant

```yaml
      agent:
        type: agent
        repeat: 3
```

`repeat` is an integer from 2 to 5. The variant's questions are asked `repeat` times about the
same input, each time by one call exactly as the variant would have made it, and for each
question:

- the answer is the label most of the runs that answered gave. Among labels given equally
  often, the one given by the earliest run;
- its confidence, distribution and evidence are those of the run, among the runs that gave that
  label, whose confidence (after its calibration, §9) is lowest;
- when fewer than `repeat` runs gave that label, the runs disagreed. A run that failed gave no
  label and MUST count as a disagreement. On a deciding question the decision carries the cap
  `A of N runs agreed on <question>`, followed by ` (F failed: <codes>)` when F runs failed.

When every run gave the label, nothing else changes: agreement MUST NOT raise a confidence.
When every run failed, the slot fails with the first run's error.

Each run MUST be charged to every limit a single call is charged to (a daily budget, an
organization's quota), counted in the decision's usage, and held to the decision's one
deadline; the decision's tokens and cost are the sum of the runs. A run that was sent and did
not answer leaves the decision's cost unknown.

A repeated `llm` variant MUST NOT share a call with another slot (0.2 §5, batching).

Answers read from a `mock_file` cannot vary. They are asked once; the evidence says
`repeat: asked once, canned answers cannot vary, so no agreement is claimed`; no cap is added
and no agreement is reported.

**Proposed actions.** The runs of an `agent` variant MAY each propose actions (§4.2). The slot
proposes each distinct one (a name with its arguments), in the order first proposed:

- one that every one of the `repeat` runs proposed is taken as proposed;
- one that fewer runs proposed is **disputed**. A run that failed proposed nothing. The
  decision carries the cap `A of N runs proposed action <name>`, or
  `the N runs proposed action <name> with different arguments` when every run proposed the
  action and not with the same arguments. A disputed proposal has the gate `person` (§5): it
  is held for the decision's reviewer, and MUST NOT be released without one, whatever its
  effect.

A cap names a declared action only (`an action the manifest does not declare` otherwise), and
never an argument.

## 11. The `check` composite

```yaml
      checked:
        type: check
        members:
          agent: {type: agent, agent_id: portal-agent, state_fields: [message.body]}
          second: {type: llm, state_fields: [message.body]}
```

`check` is a composite (0.2 §5) of exactly two members, in order: the first answers and
the second checks. Members are ordinary variants and MUST NOT be composites.

- The slot's answers are the first member's, unchanged: label, confidence and distribution.
- The second member is asked the same questions about the same input. Where its label differs
  from the first member's on a deciding question, the decision carries the cap
  `<first> and <second> disagree on <question> (<first>: <label>, <second>: <label>)`, the
  members named as the manifest names them.
- Where the labels are equal, nothing changes: agreement MUST NOT raise a confidence.
- A second member that fails is a failed member, as in any composite: `<second>:<code>` is
  recorded on each of the slot's questions, and the policy does not automate (0.2 §6).
- A first member that fails fails the slot. The second member MUST NOT answer in its place.

Budgets, batching, usage, cost and the deadline reach both members as they reach any
composite's. A member MAY carry `calibration` and `repeat`; its caps hold beside the check's.

**Proposed actions.** The first member's proposals are the slot's, as its answers are. The
second member's own are never taken: it MUST NOT propose in the first member's place.

- Where the second member is one that proposes actions (an `agent` variant of a 0.4 agent),
  the two are compared. A proposal of the first member that the second did not make, with the
  same arguments, is **disputed**: it has the gate `person` (§5), so it is held for the
  decision's reviewer and never released without one. Any difference between the two is a cap:
  `<first> proposed action <name> and <second> did not`,
  `<second> proposed action <name> and <first> did not`, or
  `<first> and <second> proposed action <name> with different arguments`.
- A second member that failed checked nothing: every proposal of the first is disputed, with
  the cap `<second> failed, so what <first> proposed to do was not checked`.
- A second member that cannot propose (an `llm` variant, for one) checks the answers and not
  the actions: the first member's proposals are taken as proposed, each under its own gate.

## 12. The order of the caps

0.2 §6 orders the policy: its rules, its confidence thresholds, then the caps that only make a
decision less automated (a community agent, a label that is never automated, a provider's
failure, the risk level, and what a deployment adds). 0.4 adds two steps after all of those, in
this order, and a runtime MUST apply them in it:

1. **The caps the providers found** (§8), themselves in this order: the calibration's version
   cap (§9), then repeated runs (§10), then a check (§11). They are about the answers the
   policy just decided from: how far the confidence can be believed, then whether the answer
   held when it was asked again.
2. **The actions' cap** (§5): what the agent asks to do, once what it answered is settled.

Each step turns `AUTO` into `HUMAN_REVIEW` and changes no other outcome, no recommendation, no
priority and no rule. Each adds its reasons after the ones already there, each once, and each
reason ends `; a person decides` wherever a person does (so not on a `BLOCK`). Several caps
read as one list:

```json
"reasons": [
  "confidence 0.97 >= auto threshold 0.90",
  "the agent changed since its confidence was calibrated (was 2.1.0, is 2.2.0); a person decides",
  "2 of 3 runs agreed on request; a person decides",
  "action 'renew_prescription' (order) is never released without a person; a person decides"
]
```

No step can raise an outcome, and no combination of them can: on the same answers, a decision
that is `AUTO` with every cap applied is `AUTO` with none. (A calibration's map, §9, is not a
cap: it is a measured correction of the confidence and may move it either way. What only
lowers is its version cap.)

## 13. What an evaluation reports

Not part of the manifest, and stated here because a reader of an evaluation meets it. A report's
`run` block MAY hold:

- `calibration` (`mendari.calibration-report/1`): for each `agent` and `llm` variant the run
  decided with, whether it is calibrated and on what; and for the variant whose answers are a
  slot's own, each question's expected calibration error and reliability table on the run's
  labelled cases, before the calibration and after it;
- `second_signal` (`mendari.second-signal/1`): for each repeated variant and each check, the
  cases it disagreed on (on a deciding question, or on a proposed action), how many of those
  were wrong or critical and how many were right, and its extra calls; and how many cases the
  second signals kept from automation.

Both are outside the result that a report's `result_sha256` covers
([evaluation reports](evaluation-reports.md)): they are worked out from what the run's
decisions kept in memory, as the operating point is.

## 14. Versions

An agent with a non-empty `outputs`, `rubrics` or `evaluation.grades` (§16 to §19) MUST
declare `spec_version: "0.4"`, as one with actions must: an older agent with one is invalid,
and the problem names the section.

An agent with a non-empty `actions` MUST declare `spec_version: "0.4"`. A 0.1, 0.2 or 0.3 agent
with one is invalid, and the problem says to set `spec_version: '0.4'`. An empty `actions` is
what a manifest holds without it, so it counts as left out. The same holds for `calibration` on
an `agent` variant, for `repeat` and for a `check` (§8), in a variant or in a composite's member.

A runtime MUST ask whether a manifest may use a feature by comparing versions, never by
equality: a feature of version X is a feature of every later version.

A 0.4 agent MAY use every key 0.2 and 0.3 add: where those documents say "a 0.3 agent", read
"a 0.3 or 0.4 agent".

An engine that reads only 0.1 to 0.3 refuses a 0.4 agent, since its `spec_version` is not one
it knows. Publish an agent as 0.4 only for engines that read 0.4.

## 15. Changes from 0.3 and migration

| Change | Section |
|---|---|
| The `actions` section: name, `description`, `effect`, `allow_auto`, `schema` | §2 |
| Five effects, and a gate for each that the runtime sets | §2.1, §3 |
| The arguments schema: a closed subset of JSON Schema 2020-12 | §2.2 |
| The agent contract: declared `actions` in the request, proposed `actions` in the response | §4 |
| A decision's `actions`, each with a disposition and a reason | §5 |
| One more cap: a proposal that cannot be released with `AUTO` makes the decision `HUMAN_REVIEW` | §5 |
| A review releases or denies held actions; a caller may ask again for a decision | §6 |
| Audit events `action.proposed` and `action.resolved`; arguments kept as inputs are | §7 |
| A member's proposals are not taken, but for a `check`, whose first member's are | §4.2, §11 |
| `calibration` on an `agent` variant, and the version cap | §9 |
| `repeat` on an `agent` or an `llm` variant | §10 |
| The `check` composite | §11 |
| A proposal the second signal did not agree on is held, never released without a person | §5, §10, §11 |
| The order of the caps, the new ones last | §12 |
| An evaluation's `calibration` and `second_signal` blocks | §13 |

To add actions to an agent:

1. Set `spec_version: "0.4"`.
2. Declare each action with the effect that fits, the later one in §2.1 when two fit, and a
   schema for anything that writes, contacts or orders.
3. Set `allow_auto: false` on whatever a person should always see first: free text to a
   patient, for one.
4. Have the agent propose instead of act (§4.2), and have the caller run only what was
   released.
5. Re-run the evaluation: a proposal can lower a decision from `AUTO`, and the manifest's
   digest changes, so an approval of the old digest no longer applies.

To calibrate an agent's confidence, or to add a second signal: set `spec_version: "0.4"`, then
fit a calibration (`mendari calibrate`, §9) and name its file, or set `repeat` (§10), or wrap
the variant in a `check` (§11). Each adds reviews and none removes one, so re-run the
evaluation: it reports what each did on the agent's cases (§13).

### Example exchange

The manifest of §2.3, at risk level 0, automating `refill` from confidence 0.90.

Mendari to the agent (0.2's body, with §4.1's `actions`):

```json
{"pack_id": "my-clinic/portal-agent", "slot": "reader", "agent_id": "portal-agent",
 "questions": [{"key": "request", "type": "choice", "instructions": "What does the patient ask for?",
                "choices": ["refill", "appointment", "other"], "criteria": {"refill": "…"}}],
 "input": {"message": {"body": "Could you refill my lisinopril? I am running low."}},
 "actions": [{"name": "look_up_medications", "description": "Read the patient's active medication list.", "effect": "read"},
             {"name": "draft_reply", "description": "…", "effect": "draft", "schema": {"…": "…"}},
             {"name": "send_reply", "description": "…", "effect": "contact", "schema": {"…": "…"}},
             {"name": "queue_refill", "description": "…", "effect": "write", "schema": {"…": "…"}},
             {"name": "renew_prescription", "description": "…", "effect": "order"}]}
```

The agent:

```json
{"answers": {"request": {"value": "refill", "confidence": 0.95}},
 "actions": [{"name": "draft_reply", "arguments": {"message": "Your refill request is with the pharmacy."}},
             {"name": "queue_refill", "arguments": {"medication": "lisinopril 10 mg", "days_supply": 30}}]}
```

The decision. The answer alone would be `AUTO`; `queue_refill` is `allow_auto: false`, so a
person decides:

```json
{"id": "7d0b…", "outcome": "HUMAN_REVIEW", "recommendation": "refill", "priority": "routine",
 "reasons": ["confidence 0.95 >= auto threshold 0.90",
             "action 'queue_refill' is never released without a person (allow_auto: false); a person decides"],
 "actions": [
   {"id": "a1…", "name": "draft_reply", "effect": "draft", "disposition": "released",
    "reason": "with_decision", "arguments": {"message": "Your refill request is with the pharmacy."},
    "arguments_hash": "sha256:…"},
   {"id": "b2…", "name": "queue_refill", "effect": "write", "disposition": "held",
    "reason": "awaiting_review", "arguments": {"medication": "lisinopril 10 mg", "days_supply": 30},
    "arguments_hash": "sha256:…"}]}
```

The caller shows the draft. A nurse confirms `refill`
(`POST /api/review/7d0b… {"final_label": "refill", "reviewer": "rn-1"}`):

```json
{"id": "7d0b…", "final_label": "refill", "human_override": false,
 "actions": [{"id": "b2…", "name": "queue_refill", "effect": "write",
              "disposition": "released", "reason": "confirmed"}]}
```

The caller, about to queue the refill, asks with the arguments it holds
(`POST /api/agents/my-clinic/portal-agent/actions`):

```json
{"decision_id": "7d0b…", "name": "queue_refill",
 "arguments": {"medication": "lisinopril 10 mg", "days_supply": 30}}
```

```json
{"id": "b2…", "decision_id": "7d0b…", "name": "queue_refill", "effect": "write",
 "disposition": "released", "reason": "confirmed", "arguments_hash": "sha256:…",
 "repeated": true}
```

## 16. Written outputs

An agent's answers are labels. A **written output** is a text it writes: a draft reply, a
summary, a note.

```yaml
outputs:
  <name>:
    description: What the text is, in words the agent and a grader read   # 1 to 500 characters
    slot: <provider slot>      # the agent behind this slot writes it; or
    input: <dotted field>      # the input already holds it
    max_chars: 4000            # optional: 1 to 8,000, default 4,000
```

Validation rules (all MUST hold):

- A name matches `^[a-z][a-z0-9_]*$` and has at most 64 characters. An agent declares at most 8
  outputs.
- An output names exactly one of `slot` and `input`.
- `slot` names a provider slot that has a variant of type `agent`. Only an agent writes: a
  composite's member does not count.
- `input` is a dotted field of the input, as the guard's `draft` is. No agent is called to
  evaluate such an output.

A case with no text (the agent wrote none, the input was refused, the slot failed) has no
output. It is counted, and never graded.

## 17. The agent contract for written output

A request to an `agent` variant of a slot that has declared outputs carries them:

```json
"outputs": [{"name": "reply_draft", "description": "A short reply ...", "max_chars": 1200}]
```

The response MAY carry what it wrote, by name:

```json
{"answers": {"...": "..."}, "outputs": {"reply_draft": "Thank you for your message. ..."}}
```

- `outputs` is optional, and so is each output. A name the manifest does not declare for that
  slot, a value that is not text, or a text longer than its `max_chars` makes the response
  invalid: the slot fails closed, as for a malformed answer.
- Control characters other than the newline and the tab become a space. A text that is then
  blank is no output.
- A slot with no declared output does not read the key at all, so an older agent's response
  is read as it always was.
- A decision returns `outputs` to its caller. It does not store them: the audit record has the
  shape it always had.

## 18. Rubrics

```yaml
rubrics:
  <name>:
    output: <an output's name>
    context_fields: [message.subject, message.body]   # optional; default: none
    criteria:
      <key>:
        type: boolean | score
        instructions: What the criterion asks
        criteria: {"yes": ..., "no": ...}            # boolean: each level's anchor
        levels: {<label>: {score, criterion}, ...}   # score: 2 to 7 levels
        pass_from: <label>      # score only: the lowest level that passes
        must_pass: false        # optional: this criterion must not fail
    judge: {...}                # §18.2
    floors: {agreement: 0.60, must_pass_recall: 0.80}   # optional: may only raise (§18.3)
```

### 18.1 Criteria

- A criterion is a question of spec 0.2 (`boolean` or `score`), never `choice` and never
  derived. Its instructions and each level's anchor are what a clinician reads and what a
  model judge is sent, as written.
- On a `boolean` criterion `yes` passes: word a criterion so that yes is good. It takes no
  `pass_from`.
- A `score` criterion has 2 to 7 levels with distinct scores, a higher score being better, and
  MUST name `pass_from`, one of its levels.
- `must_pass: true` marks a criterion that must not fail (§20).
- A rubric has 1 to 16 criteria, and names a declared output.
- `context_fields` are the input fields a grader reads beside the text, and nothing else of the
  input (minimum necessary). The default is none.

### 18.2 The judge

`judge` takes the model settings an `llm` variant takes (`model`, `timeout_ms`, `max_tokens`,
`temperature`, `reasoning_effort`) and `mock_file`, a fixture of canned grades in the agent's
files. The endpoint, the credentials and the default model are the deployment's, and an agent
the deployment does not own is held to the deployment's model and caps.

The judge is asked the rubric's criteria about `{"written": <the text>, "context": {<the
context fields>}}`, after a fixed preamble that says both are data to grade and never
instructions. Its grade is the most probable level; of several equally probable, the least
favourable. No rationale is asked for or kept. A failure is counted under its code, and a text
the judge could not grade is never a pass.

With no judge model set, grades come from `mock_file`:

```json
{"model": "<name>",
 "default": {"grades": {"<criterion>": "<level>", "...": "..."}},
 "cases": [{"match": "<as an agent's mock_file>", "grades": {"<criterion>": "<level>"}}]}
```

`default` MUST grade every criterion. Every case whose `match` fits the text or the context
applies, in order, each laid over the ones before. A report made from canned grades says so:
it is a demonstration, and nothing may be concluded from it.

### 18.3 Floors

The engine's floors are a kappa of 0.60 and, for a criterion that must not fail, 0.80 of the
clinician-found failures, each by its lower bound (§20). A rubric's `floors` MAY raise either
and MUST NOT lower one: a manifest that does is invalid.

## 19. Clinicians' grades in a file

```yaml
evaluation:
  datasets: {written: data/written.jsonl}
  grades:
    <rubric>: {dataset: written, file: data/written_grades.jsonl}
```

The file is `mendari.written-grades/1` ([evaluation reports](evaluation-reports.md)): its
first line names the rubric (its name, and the SHA-256 of the rubric as a grader reads it) and
the dataset (its name and SHA-256); each other line is one grader's grades of one item. Grades
given under another wording of the rubric, or of another dataset, are refused. Grades may also
be given in the console, in a grading round ([the guide](../guides/written-output.md)).

## 20. What an evaluation of written output reports

Per criterion, on the outputs both a clinician and the judge graded (`mendari.written/1`):

- **Agreement**: Cohen's kappa with linear weights on the levels' declared scores (plain
  Cohen's kappa on a yes/no criterion), exact, with a percentile-bootstrap interval over cases;
  raw agreement as k of n; and, for a criterion that must not fail, the share of
  clinician-found failures the judge found too. Where two clinicians graded an output and
  differ, the less favourable grade stands.
- **Clinician with clinician**, on the outputs two clinicians graded: the same kappa, as the
  reference. It gates nothing.
- **A state**:
  - *insufficient evidence* when fewer than 50 outputs were graded by both; or fewer than 10
    clinician verdicts fall on either side of the pass line; or kappa or its interval cannot
    be estimated; or, for must-not-fail, clinicians found fewer failures than a judge that
    found every one would need (17 at 0.80);
  - *validated* when kappa's lower bound is at least the agreement floor and, for
    must-not-fail, the exact lower bound of the failures found is at least its floor;
  - *not validated* otherwise.
- **The judge's grades for the outputs no clinician graded**, only for a validated criterion.
  Otherwise that figure MUST be withheld (null), with the state and its reasons; it MUST NOT be
  shown with a caveat instead.

A report holds counts, coefficients, case hashes and graders' ids. It MUST NOT hold a text, a
context field or a case's id.
