# Governing actions

An agent's answers say what it concluded. Its **actions** are what it asks to have done: a
message put in a queue, a note filed, a reply sent to a patient, an order placed. Mendari
governs both. The agent's manifest declares what it may do; the agent proposes actions with its
answers; Mendari answers each with `released`, `held` or `blocked`; and every proposal and its
resolution is in the audit trail.

**Mendari authorizes. It never executes.** It holds no credential to a chart or to a patient
channel. Your caller, the code that runs the agent's tools, runs an action, and only one that
was released.

This guide walks through it with the bundled demo agent, `agents/portal-message-agent`. The
rules are in [the agent manifest spec](../spec/agent-manifest.md#13-actions), section 13, and
the reasons in [ADR-0023](../decisions/ADR-0023-an-agent-proposes-actions.md) and, for free
text, [ADR-0027](../decisions/ADR-0027-no-free-text-is-released-unread.md).

## See it in a minute

```bash
uv run mendari demo
```

Between its messages and its review queue, the demo shows what its agent asked to do:

```text
== 2. What the agent asks to do ============================================
Mendari authorizes each action and runs none: your caller runs what is released.
released  assign_queue (write)                 with the AUTO decision on message 1
held      send_acknowledgement (contact)       message 3 waits for a reviewer
released  send_acknowledgement (contact)       the reviewer confirmed it
blocked   an action the agent never declared   asked for message 1
```

A write was released with an automated decision. A message to the patient was held until a
reviewer confirmed the decision. An action the agent never declared was blocked.

## 1. Declare what the agent may do

Add an `actions` section to `agent.yaml`. The demo agent's:

```yaml
actions:
  assign_queue:
    description: Put the message in the refill, scheduling or front-desk queue.
    effect: write
    schema:
      type: object
      additionalProperties: false
      required: [queue]
      properties:
        queue: {type: string, enum: [refill, scheduling, front_desk]}
  send_acknowledgement:
    description: Send the patient a short acknowledgement that the clinic received the message.
    effect: contact
    allow_auto: false
    schema:
      type: object
      additionalProperties: false
      required: [template]
      properties:
        template: {type: string, enum: [received]}
```

Each action has:

- a **name** (lowercase letters, digits and underscores, at most 64 characters);
- a **description** a reviewer can read: it is shown on the Decision Card and sent to the agent
  as written;
- an **effect**, one of five (below);
- optionally a **schema** for its arguments: JSON Schema, from a small closed set of keywords
  (types, `properties`, `required`, `enum`, `const` and bounds; every object sets
  `additionalProperties: false`). Give one to anything that writes, contacts or orders, and
  make every string an `enum` or a `const` wherever the choices are known: an action that
  takes free text is never released without a person ([section 3](#3-the-gate));
- optionally **`allow_auto: false`**: the action is then never released without a person.

`mendari validate` checks the section. `mendari new` writes a commented example of it into a
new agent's manifest. An agent declares at most 16 actions.

## 2. What each effect means

| Effect | The action | Example |
|---|---|---|
| `read` | reads data and changes nothing | look up the medication list |
| `draft` | prepares something a person sees before it takes effect | draft a reply for staff to send |
| `write` | changes the chart or another system of record | put a message in a queue, file a note |
| `contact` | sends something to a patient, or to anyone outside the care team | send a portal reply |
| `order` | places an order or a prescription, or changes treatment | renew a prescription |

An action that fits two takes the later one. The effect is your declaration, as the risk level
is: Mendari cannot check what a tool does, so whoever adopts the agent reads its effects on the
Decision Card before approving it.

## 3. The gate

What each effect needs before an action is released is the runtime's rule. It lives beside the
risk caps, not in your manifest: a manifest can tighten it, and nothing in a manifest or in
what an agent proposes loosens it.

| The decision is | `read`, `draft` | `write`, `contact` with enumerated arguments | `order`; any action with `allow_auto: false`; `write`, `contact` that can hold free text |
|---|---|---|---|
| `AUTO` | released | released | the decision becomes `HUMAN_REVIEW`: held |
| `HUMAN_REVIEW`, with a recommendation | released | held for the reviewer | held for the reviewer |
| `ABSTAIN` (no recommendation) | released | blocked | blocked |
| `BLOCK` | blocked | blocked | blocked |

Four things follow from the table:

- **`write` and `contact` are released without a person only where the agent may automate at
  all.** That is risk levels 0 and 1; levels 2 and 3 once your organization has recorded a
  governance approval for the agent's digest; level 4 never. An automation hold, a provider
  failure, or canned answers on a server with sign-in stop automation, and so stop these
  releases with it.
- **An `order` never goes without a person.** Proposing one, or any action with
  `allow_auto: false`, sends an otherwise automated decision to a reviewer. That is the only
  way the action gets a person, and it only lowers automation.
- **Free text never goes without a person.** A `write` or a `contact` whose arguments can
  hold free text is treated as an `order` is, whatever your manifest says: nobody has read
  the words, so no automated decision sends them to a chart or to a patient. Proposing one
  sends the decision to a reviewer, with the reason:

  ```text
  action 'send_reply' can carry free text and is never released without a person; a person decides
  ```

- **An abstention blocks side effects at once.** With no recommendation there is nothing for a
  reviewer to confirm.

**What counts as free text.** An action's arguments can hold free text when its schema takes a
string that is not limited to an `enum` or a `const`, or a list with no `items`, or when the
action has no schema at all. A length limit does not help: a dose fits in a few characters.
The schema has no pattern keyword, so an identifier that is not enumerated counts too. A `read`
or a `draft` with such arguments is still released with the decision: a read changes nothing,
and a person sees a draft before it takes effect.

**How to send a text, then.** Through an action a person releases. The demo agent's
`send_acknowledgement` is the model: `allow_auto: false`, and one enumerated argument, the
name of a template. The words themselves are a written output
([Evaluating written output](written-output.md#2-who-gets-the-text)): the reviewer reads the
draft, releases it or withholds it, and your caller sends it only once the action is
released. An action that should go without a person takes enumerated arguments only, as the
demo agent's `assign_queue` does.

Two more rules:

- **What the agent never declared is blocked**, and so are arguments that do not match the
  action's schema. Either one also sends the decision to a person: an agent that proposes what
  it never declared is not trusted to automate.
- **An agent your deployment has not vouched for gets nothing released without a person**: one
  from a registry before your organization approves its digest. Its
  declared effects are claims nobody here checked, so even its reads wait for a reviewer.
- **An action the agent was not sure of is held, never released.** Where the agent is asked
  each case several times, or checked by a second agent
  ([Calibration and a second signal](calibration-and-a-second-signal.md)), a proposal that only
  some runs made, or that the second agent did not make with the same arguments, waits for the
  decision's reviewer whatever its effect, a `read` too. The decision goes to a person, and
  its reasons say which action the runs differed on:

  ```text
  2 of 3 runs proposed action 'look_up_medications'; a person decides
  ```

  A second model that only answers the questions (an `llm` variant) checks the answers and not
  the actions: the agent's proposals are then gated by the table above, as always.

## 4. Propose, do not act

A request to a 0.4 agent carries the actions its manifest declares. The agent answers the
questions as before, and may add the actions it proposes:

```json
{"answers": {"request": {"value": "refill", "confidence": 0.95},
             "urgent_symptoms": {"value": "no", "confidence": 0.97}},
 "actions": [{"name": "assign_queue", "arguments": {"queue": "refill"}}]}
```

With the agent kit, `respond` takes them:

```python
from mendari.agent_kit import Case, respond

def handler(case: Case) -> dict:
    return respond(
        case,
        {"request": ("refill", 0.95), "urgent_symptoms": ("no", 0.97)},
        actions=[("assign_queue", {"queue": "refill"})],
    )
```

Proposing is asking. The agent must not run an action that writes, contacts or orders before
Mendari releases it. A response may propose at most 16 actions.

The decision lists each one with what may be done with it:

```json
{"id": "7d0b…", "outcome": "AUTO", "recommendation": "refill",
 "actions": [{"id": "a1…", "name": "assign_queue", "effect": "write",
              "disposition": "released", "reason": "automated",
              "arguments": {"queue": "refill"}, "arguments_hash": "sha256:…"}]}
```

## 5. Run only what was released

Read `disposition`, never only the status code or the outcome:

- `released`: run it.
- `held`: do not run it. It waits for the decision's reviewer.
- `blocked` (or `denied`, a reviewer's refusal): do not run it.

Right before running a tool, ask again. The call is safe to repeat: the same action with the
same arguments for the same decision answers how it stands now, and records nothing new. It is
how a caller that holds a held action learns that a reviewer released it, and how a caller
checks that the arguments it is about to use are the ones that were released.

```bash
curl -s -X POST http://127.0.0.1:8000/api/agents/mendari/portal-message-agent/actions \
  -H 'content-type: application/json' \
  -d '{"decision_id": "7d0b…", "name": "assign_queue", "arguments": {"queue": "refill"}}'
```

```json
{"id": "a1…", "decision_id": "7d0b…", "name": "assign_queue", "effect": "write",
 "disposition": "released", "reason": "automated", "arguments_hash": "sha256:…",
 "repeated": true}
```

A blocked action is not an HTTP error: the answer is a `200` with `disposition: "blocked"`. A
decision that is unknown, another organization's or another agent's is a `404`. The call needs
what deciding needs: the `decide` permission, and for a scoped key the scope that reaches
`…/decide`.

From Python, the kit's `authorize` asks, and fails closed: if Mendari cannot be reached or
gives an answer it cannot trust, the action is not released.

```python
from mendari.agent_kit import authorize

found = authorize("mendari/portal-message-agent", decision_id, "assign_queue", {"queue": "refill"})
if found.released:  # MENDARI_URL and MENDARI_KEY say where to ask
    assign_queue(queue="refill")
```

An assistant that speaks MCP calls the tool `authorize_action` of `mendari mcp` with the
decision's id, the action and its arguments, and acts only on `released`.

You may also ask about an action the agent did not propose with its answer. It is checked and
gated by the decision as it stands, and recorded with who asked:

| The decision | Asked for afterwards |
|---|---|
| automated | `read`, `draft`, and a `write` or a `contact` with enumerated arguments: released. `order`, any action with `allow_auto: false`, and a `write` or a `contact` that can hold free text: blocked, since nobody is waiting to confirm it |
| automated, and automation is now on hold or its approval is gone | `write`, `contact`: blocked |
| waits for review | held with the rest, and resolved by the same review |
| already reviewed | `write`, `contact`, `order`: blocked, since the reviewer never saw it |

After an action is refused for what it is (undeclared, or arguments that fail its schema), no
further `write` or `contact` is released on that decision.

## 6. Review releases or denies

A decision in the review queue shows its proposed actions, each with its effect and what may
be done with it, and the texts the agent wrote for it. When the reviewer resolves the
decision, its held actions are resolved with it:

- **choosing another label denies them all**, whatever else the review says;
- **confirming the recommendation releases them**, but **free text only by name**. Two kinds
  of held action are released only when the review names them, and are denied otherwise
  (`not_named`):
    - an action whose arguments can hold free text (the queue marks it `free_text`), whatever
      made it wait: the reviewer has to have read what it says;
    - every held action, once the reviewer withholds a text the agent wrote: your manifest
      does not say which action uses which text;
- a reviewer who agrees with the label and not with an action **refuses that action**: it is
  denied (`reviewer_denied`), and the review is still recorded as a confirmation, never as a
  false override. Leaving an action unnamed is no override either.

Over the API, `POST /api/review/{id}` takes, beside `final_label` and `reviewer`:

| Field | What it says |
|---|---|
| `texts` | The reviewer's verdict on each text the decision holds, by the output's name: `release` or `withhold`. Every held text needs one |
| `release_actions` | The ids of the held actions to release by name |
| `deny_actions` | The ids of the held actions to refuse while confirming the label |

The demo agent's decision on "Did my lab results come back yet?" waits for a person. It holds
the acknowledgement the agent drafted and two actions, `assign_queue` and
`send_acknowledgement`, both with enumerated arguments. A reviewer who agrees with the label
but not with the draft withholds the text, and names the queue assignment so that it still
goes:

```json
{"final_label": "other", "reviewer": "rn-1",
 "texts": {"acknowledgement": "withhold"}, "release_actions": ["a1…"]}
```

```json
{"id": "7d0b…", "final_label": "other", "human_override": false,
 "outputs": [{"name": "acknowledgement", "verdict": "withheld"}],
 "actions": [{"id": "a1…", "name": "assign_queue", "effect": "write",
              "disposition": "released", "reason": "confirmed"},
             {"id": "b2…", "name": "send_acknowledgement", "effect": "contact",
              "disposition": "denied", "reason": "not_named"}]}
```

Had the reviewer released the text (`"texts": {"acknowledgement": "release"}`) and named
nothing, both actions would have been released: neither takes free text.

**If your caller only confirmed labels.** A review that sent `final_label` and `reviewer` and
relied on the release has to say more now:

- For a decision that holds a text (`outputs` on its item in `GET /api/review`), send `texts`
  with a verdict for each. Without them the review is refused (`409`), and the refusal names
  the outputs.
- For a held action marked `free_text` in the queue, send its id in `release_actions`.
  Without it the review succeeds and the action is denied: read `actions` in the answer.
- Once you withhold a text, name every action you still want released.
- A decision with no text and no free-text action is reviewed as before.

An id in `deny_actions` or `release_actions` that is no held action of the decision, or one
in both, refuses the whole review (`409`), and nothing is recorded. Whatever sends the review,
the console's review screen included, the server holds it to these rules: nothing releases a
text or a free-text action that its reviewer did not choose to release.

**In the console.** A decision's card in the review queue shows each held text under its
output's name with two choices, *Release as written* and *Withhold*, neither chosen: the
labels wait until every text has its verdict. Each held action has a tick. One whose arguments
can hold free text starts unticked, and withholding a text unticks every action, the
reviewer's own ticks too: what is released after that is what they ticked after that. Because
a verdict and a release cannot be taken back, the console waits five seconds, with Undo, before
it sends a review that gives either. A release is final: a hold set afterwards stops new
releases, not one already given.

## 7. What the audit trail shows

Every proposal is an event on its decision, `action.proposed`, chained with the decision
itself; every held action's resolution is an `action.resolved`. Each holds ids, the declared
name, the effect, the disposition, a reason code, a hash of the arguments, and who asked or
who reviewed. A proposal whose arguments can hold free text also says so (`free_text`), which
is how a review knows it must name the action.

The chain never holds the arguments, and never a name the manifest does not declare: both are
the agent's text and can hold patient data. They are kept apart from the chain, exactly as the
decision's input is:

| PHI mode | The arguments |
|---|---|
| `encrypt` | sealed; reviewers and administrators read them, and each read is chained |
| `hash`, or an agent whose `audit.store_input` is `hash` | not kept; the chain has their hash |
| `off` (a local sandbox) | kept beside the chain, readable by whoever may read the decision |

Erasing a decision's input erases them, and retention removes them, each with its own chained
event. `mendari audit verify --notes` checks each kept set against the commitment its event
chained. The audit view of the console lists a decision's actions above its events, and the
FHIR export carries both event kinds, as codes only.

## 8. On the Decision Card

An agent that declares actions has an **Actions** section on its Decision Card: each action,
what it does, its effect, how it is released for an agent your deployment vouches for, and the
arguments that are checked. An action whose arguments can hold free text says so there, and
that a reviewer has to name it. Read the section before you approve an agent: an approval
names the digest that card describes, and at risk levels 2 and 3 the approval that lets the
agent automate also lets its `write` and `contact` actions of enumerated arguments go without
a person.

## What comes next

This is the first phase. Planned for the second:

- asking mid-run, before a decision exists (a run as a subject of its own in the audit chain),
  and reads the agent already made, reported and checked;
- revoking a release, expiry of held and released actions, and the caller's report that it ran
  one;
- reviewing single actions on a decision that is not in the queue, and a permission of its own
  for releasing orders;
- `expect.actions` in behaviour tests, and action figures in evaluation reports (how often a
  proposal is refused or held);
- actions in CDS Hooks answers, in `$decide`, and on the FHIR Task;
- the full input-schema keyword set for arguments, and schemas in files;
- a deployment's own tightening per effect.
