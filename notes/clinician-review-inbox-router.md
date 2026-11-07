Drafted by a build session for a clinician to review. Not clinically reviewed.

# The inbox router's red flags, urgency criteria and risk level: a brief for the clinician reviewer

_Written 2026-11-07 (US Eastern). For a triage nurse (launch prerequisite B21.1; launch gate
`clinician-review`). On 2026-11-07 the owner, a licensed physician who wrote the five reference
packs, decided that their own disclosed physician review counts for these five packs. Nothing
here has been reviewed by a clinician. The review is recorded by the owner in the gate
`clinician-review`'s attestation, never by a build session._

## What this brief covers, and the pack it describes

| | |
|---|---|
| Pack | `mendari/patient-inbox-router` |
| Version | 0.4.3 |
| Digest | `sha256:d076c4d9e59eda856fa8246daa8e2aea3f5533fe7523daaf61024871f593dfc2` |
| Read with | `uv run mendari validate packs/patient-inbox-router`, on `main` at `af26a868` |

Covered: the urgency criteria (what counts as a possible emergency), the keyword red-flag list
that screens for them offline, what an escalation does, and the pack's level-0 risk rationale,
with one question on the team criteria. If any pull request changes the pack after this brief,
its version and digest change: re-read the quoted lines and refresh this brief before the review
is recorded.

## What the pack does, and who sees its result

Each incoming patient portal or SMS message (a FHIR Communication) gets two answers
(`pack.yaml:77-119`):

- **Team** (`destination`): front desk, medical assistant, nurse, physician or billing, by a
  statistical router trained on synthetic messages (`pack.yaml:165-198`).
- **Possible emergency** (`urgency`): urgent or routine, by the safety screen
  (`pack.yaml:121-164`). Offline it is keyword rules (`logic/red_flags.yaml`); a server with
  Jev set up runs the rules and Jev together, and either one flagging escalates.

Who sees the result:

- A message the screen flags (P(urgent) >= 0.35) goes to **a nurse, at stat priority, for a
  person to triage now**, whatever team the router chose (`policy.yaml:27-38`). Its EHR card is
  critical (`pack.yaml:231-233`).
- Otherwise the message is routed to its team's work queue as a FHIR Task
  (`fhir/mapping.yaml:29-31`). With a model in the screen, a confident message may be routed
  with no one confirming the team (AUTO at 0.90, 0.95 for front desk and billing;
  `policy.yaml:9-19`). **With the keyword rules alone, nothing is automated**: every message
  goes to a person (`policy.yaml:21-25`).
- An unsure message stays in the shared inbox for manual triage by the front desk
  (`policy.yaml:40`).
- The pack never replies to patients. Portals must keep telling patients to call 911 for an
  emergency (`pack.yaml:36-39`).

## The urgency criteria, exactly as built

The question is "Could the situation in this message be a medical emergency that is happening
now or getting worse?" (`pack.yaml:104-106`). A model reading the message reads these words;
the keyword rules below are narrower.

| Label | Criteria (verbatim) | Where |
|---|---|---|
| urgent | Possibly an emergency now, even when described casually or with uncertainty: chest pain or pressure; trouble breathing; stroke signs such as face drooping, arm weakness or slurred speech; thoughts of suicide or self-harm, or an intentional overdose; heavy bleeding, including vomiting or coughing up blood, or bleeding in pregnancy; a severe allergic reaction; fever in a baby under 3 months; a person who is hard to wake, fainting or newly confused; a head injury followed by vomiting or confusion; sudden severe pain in the head or belly. | `pack.yaml:108-115` |
| routine | Not an emergency now: administrative requests, refills, questions, mild or long-standing symptoms, and past episodes that have already been treated or assessed, such as asking for records from an emergency room visit. | `pack.yaml:116-119` |

## The keyword red-flag list, exactly as built

`logic/red_flags.yaml`. Routine starts at a score of 2.0; each matching pattern adds its weight
to urgent; a message escalates when P(urgent) >= 0.35, which any single pattern of weight about
1.4 or more reaches (`red_flags.yaml:5-7`, `:23-24`). A negatable pattern does not count when "no",
"not", "denies", "denied", "without", "never" or "sin" comes within 40 characters before it
(`:15-16`). Words of 5 letters or more tolerate a typo ("chset pain", `:17-19`). The phrases
below summarise each pattern; the exact pattern is at the line given.

| Red flag | Weight | Negatable | Matches, in short | Line |
|---|---|---|---|---|
| chest-pain | 6 | yes | chest pain, pressure or tightness; tight or crushing chest; pain in my chest; "elephant" near "chest"; *dolor de/en el pecho* | `:27-30` |
| breathing-difficulty | 6 | yes | can't breathe or catch my breath; trouble, difficulty or struggling to breathe; short of breath; out of breath; can barely breathe or talk; lips turning blue; gasping; asthma attack; *falta el aire*, *no puedo respirar* | `:31-34` |
| stroke-signs | 6 | yes | face drooping or numb; slurred; can't find words, words jumbled or garbled; sudden weakness, numbness or confusion; can't lift or move an arm or leg; can't walk straight; *cara caída*, *no puede hablar* | `:35-38` |
| self-harm | 7 | **no** | kill myself; end it, end my life; suicid-; don't want to live or be alive; hurt myself; *quitarme la vida* | `:39-41` |
| intentional-overdose | 5 | **no** | took a lot, way or much more; on purpose; overdos-; swallowed pills, tablets or a bottle; a whole or entire bottle of | `:42-44` |
| severe-bleeding | 6 | yes | won't stop bleeding; bleeding heavily; heavy bleeding; throwing, vomiting or coughing up blood | `:45-48` |
| gi-bleeding | 6 | yes | vomiting or throwing up blood; blood in vomit or stool; coffee grounds; black, tarry stool; blood in the toilet; *vomitando sangre*, *vómito con sangre* | `:49-52` |
| coughing-blood | 6 | yes | cough- near blood; hemoptysis | `:53-56` |
| pregnancy-bleeding | 6 | yes | pregnan- and bleed- within a few words, either order | `:57-60` |
| anaphylaxis | 6 | yes | throat closing, swelling or tight; swelling of the lips, tongue or throat; hives near throat or breathing | `:61-64` |
| near-fainting | 5 | yes | going or about to pass out; gonna pass out; feel (very) faint | `:65-68` |
| hard-to-wake | 5 | yes | hard to wake; won't wake; unresponsive; passed out; fainted | `:69-72` |
| head-injury | 6 | yes | hit, bumped or banged their head, then throwing up, vomiting, confused or drowsy | `:73-76` |
| acute-abdomen | 5 | yes | belly, abdomen or stomach rock- or board-hard; severe abdominal, belly or stomach pain | `:77-80` |
| worst-headache | 6 | yes | worst headache of my life; thunderclap | `:81-84` |
| young-infant | 1.2 | no | "N weeks old", "N days old", newborn, infant | `:85-87` |
| fever | 1.0 | yes | fever; temperature; a number from 100 to 104.9; *fiebre* | `:88-91` |

Young-infant and fever escalate only together (1.2 + 1.0 = 2.2, P(urgent) 0.55); either alone
does not.

**What the rules do on a few phrasings** (run on the pack as built, `mendari decide
packs/patient-inbox-router --text ...`):

| Message | Escalated by the rules |
|---|---|
| "My 6 weeks old has a fever of 100.6" | yes |
| "My 8 weeks old baby has a temperature of 100.1" | yes |
| "My 9 month old infant has a fever of 101" | yes ("infant") |
| "My 6-week-old has a fever of 100.6" | **no** (the hyphenated form is not matched) |
| "My 2 month old has a fever of 100.6" | **no** (months are not matched) |
| "My son had a seizure this morning and is still sleepy" | **no** (seizure is not in the criteria or the rules) |
| "My blood sugar is 45 and I feel shaky and confused" | **no** |
| "I suddenly cannot see out of my left eye" | **no** |
| "I am not suicidal, I just need my sertraline refilled." | yes (self-harm is not negatable) |
| "I had chest pain last year, can I get my records?" | yes (past symptoms over-trigger, `pack.yaml:49`) |

On the hand-written held-out emergencies, the rules alone caught 4 of 20 and rules with Jev
caught 20 of 20 (measured on earlier versions; `DECISION_CARD.md:221`, `:223`). With the rules
alone nothing is automated, so a missed emergency still reaches a person, at routine priority.

## The risk level, exactly as built

| | |
|---|---|
| Level | 0, administrative (`pack.yaml:13-15`) |
| Rationale (verbatim) | "The decision is which team reads a message first. The pack never replies to patients or makes a clinical judgement. A misroute delays a message but does not by itself change care. A message the urgency screen flags as a possible emergency is never auto-routed: it goes to a nurse for stat human review. Nothing is automated unless a model screens beside the keyword rules, which miss emergencies phrased in ways they do not list." (`pack.yaml:16-21`) |

What each level means at runtime (`src/mendari/core/spec.py:203-208`,
`src/mendari/core/policy.py:28-29`, ADR-0002):

| Level | Name | Automation |
|---|---|---|
| 0 | administrative | Allowed (within the pack's policy) |
| 1 | clinical information | Allowed; the same as level 0 at runtime |
| 2 | clinical support | Every decision goes to a person, unless the site records a governance override |
| 3 | patient prediction | As level 2 |
| 4 | time-critical | Never automated |

A message the screen does **not** flag (a missed emergency) can, with a model in the screen, be
routed with no one confirming the team, including to the front desk or billing. Whether a
delayed message "does not by itself change care" is the clinical judgement this rationale rests
on.

## Questions for the reviewer

Pick one option per question (any answer may instead be written in). The current behaviour is
marked "(as built)". Where a value looks clinically questionable, the note says why; nothing
here is a recommendation.

**I1. The urgent list.** Not in the criteria as built: seizure, sudden loss of vision, severe
low blood sugar, thoughts of harming others, and pregnancy warning signs other than bleeding.
- a. As built.
- b. Add seizure, sudden loss of vision, and severe low blood sugar with symptoms.
- c. As b, and add thoughts of harming others, and in pregnancy: decreased fetal movement, or a severe headache or vision change.

**I2. Which messages count as about a young infant.** As built, "N weeks old", "N days old",
"newborn" and "infant"; "6-week-old" and "2 month old" are missed, and "infant" also matches a
9-month-old.
- a. As built.
- b. Also the hyphenated forms ("6-week-old") and "1 month old" or "2 months old".
- c. Treat a fever in any baby under 12 months as urgent.

**I3. What counts as a fever in a young infant.** As built, any mention of fever or
temperature, or a number from 100 to 104.9 (so 100.1 counts).
- a. As built.
- b. 100.4 °F (38.0 °C) or above, or a reported fever with no number.

**I4. Negation of self-harm and overdose.** As built, these two are not negatable: "I am not
suicidal" escalates to stat nurse triage. All other red flags are negatable.
- a. As built (any mention escalates).
- b. Negatable like the others.

**I5. How much doubt escalates.** As built, P(urgent) >= 0.35 goes to stat nurse triage.
- a. 0.35 (as built).
- b. 0.20 (more false alarms, fewer misses).
- c. 0.50 (fewer false alarms).

**I6. Controlled-substance refills.** As built, routine refills and renewals go to a medical
assistant (`pack.yaml:88-90`), whatever the drug.
- a. As built.
- b. Refills of controlled substances (opioids, benzodiazepines, stimulants) go to the physician.
- c. Refills of controlled substances go to a nurse.

**I7. The risk level.**
- a. Level 0, rationale as built.
- b. Level 0, with the rationale saying that a missed emergency routed to a non-clinical team can delay care.
- c. Level 1 (clinical information): the same automation, named as clinical.
- d. Level 2 (clinical support): every message confirmed by a person unless a site records a governance override.

**I8. Verdict.**
- a. Acceptable as a reference pack.
- b. Acceptable with the changes answered above.
- c. Not acceptable (say why).

## The review

To be filled in by the reviewer, and recorded by the owner in gate `clinician-review`'s
attestation. A build session leaves it blank. The reviewer's name is not written here: the gate
keeps it in a note outside the repository.

| Field | Value |
|---|---|
| Reviewer's role (triage nurse, physician, other) | |
| Date | |
| Pack and version reviewed | |
| Pack digest reviewed (`mendari validate packs/patient-inbox-router`) | |
| Answers to the questions above | |
| Verdict (acceptable, acceptable with changes, not acceptable) | |
