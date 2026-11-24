# Agent manifest spec 0.4

Status: draft. Adds actions to [0.3](agent-manifest-0.3.md): what an agent may ask to have done
besides answer, and how the runtime gates it. Every other part of an agent is as
[0.2](agent-manifest-0.2.md) and 0.3 say, and a 0.4 manifest without `actions` is a 0.3 manifest
with `spec_version: "0.4"`. Every valid 0.1, 0.2 and 0.3 agent is still valid, with unchanged
behaviour (§8). The key words MUST, SHOULD and MAY are used as in RFC 2119. The reference
implementation is `mendari.core.spec` (the section), `mendari.core.policy` (what an effect
needs) and `mendari.core.actions` (checking a proposal). The decision behind it is
[ADR-0023](../decisions/ADR-0023-an-agent-proposes-actions.md).

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
- two audit events (§7).

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
- An agent that declares any action MUST declare `spec_version: "0.4"` (§8).

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

A composite's member (`any_of`, `cascade`, `vote`) MAY be an agent, but its proposals are not
taken in this version: each is blocked (`composite_member`), and the decision is not automated.

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
or an action whose gate is `person`. The reasons say which, naming only declared actions. The
recommendation and the priority do not change, a decision already going to a person keeps its
outcome (and gets the reason), and a `BLOCK` stands. Then each proposal's disposition follows
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

## 8. Versions

An agent with a non-empty `actions` MUST declare `spec_version: "0.4"`. A 0.1, 0.2 or 0.3 agent
with one is invalid, and the problem says to set `spec_version: '0.4'`. An empty `actions` is
what a manifest holds without it, so it counts as left out.

A 0.4 agent MAY use every key 0.2 and 0.3 add: where those documents say "a 0.3 agent", read
"a 0.3 or 0.4 agent".

An engine that reads only 0.1 to 0.3 refuses a 0.4 agent, since its `spec_version` is not one
it knows. Publish an agent as 0.4 only for engines that read 0.4.

## 9. Changes from 0.3 and migration

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
| A composite member's proposals are not taken | §4.2 |

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
