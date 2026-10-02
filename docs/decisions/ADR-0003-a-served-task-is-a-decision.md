# ADR-0003: A FHIR Task served by Mendari is a decision, not the work item it routes

## Status
Accepted (Phase 3 of the FHIR work, 2026-10-02)

## Context
A decision produces two things a FHIR client may call a Task. One is the **work item** the
decision proposes: "route this message to the nurse queue", with the destination team as
`performerType`. `$decide` returns it, and an EHR creates it on its own server (the sandbox worker
does so on HAPI). The EHR then owns it: received, in progress, completed.

The other is **the decision itself**: Mendari evaluated a case, and either the answer was final
at once or it waits for a person in Mendari's review queue. Mendari holds this state, in the
hash-chained audit trail.

Phase 3 makes decisions readable and searchable as FHIR. Serving the work item would make
Mendari a second, stale copy of a resource the EHR owns: it would still say `ready` long after
the EHR completed the work, because Mendari never sees that. Mendari is not a workflow server.

## Decision
- `GET /fhir/Task/{id}` and `GET /fhir/Task?…` serve **one Task per Mendari decision**, for
  every decision (made through FHIR or not). The id is the decision id.
- Status says what Mendari knows: `requested` while the decision waits for a reviewer in
  Mendari's queue, `completed` once the answer is final (at once, or after the review), `failed`
  when the input was refused before any provider ran. `businessStatus` is the outcome (AUTO,
  HUMAN_REVIEW, ABSTAIN, BLOCK).
- The Task is derived on read from the decision record and its chain events: nothing new is
  stored. Its versions are the events that change it (created, review resolved, outcome
  recorded), so `meta.versionId` and vread follow the chain.
- It carries **no input text and no subject identifiers**: no `for`, no `focus`, no review or
  feedback notes. Patient, subject and focus search are refused (`400`), so a client never gets
  every patient's decisions while it believes it filtered by one.
- Read-only. No create, update, delete, conditional read or `_history` Bundles (a history
  Bundle would have to describe REST interactions that never happened).
- The work item keeps the same `decision-id` identifier, as `$decide` returns it today. The
  CapabilityStatement says which Task is which.

## Consequences
- Every status Mendari serves is one it can keep true.
- A client that merges the EHR's server and Mendari's sees two Tasks with one identifier, with
  different meanings. Documented; see the alternative below for the clean fix.
- The derivation is code: a release that changes it changes how an existing version renders.
  Tests pin the rendered JSON for a fixed chain, and any change is a changelog item.

## Alternatives Considered
- **Serve the work item.** Rejected: a stale second source of truth for a resource the EHR owns.
- **No Task, only AuditEvent and Provenance search.** Rejected: there would be no FHIR view of a
  decision's current state or of the review queue.
- **Store patient references to allow patient search.** Rejected for now: a new PHI surface
  (plain text under PHI mode `off`, a keyed hash under `encrypt`, impossible under `hash`).
- **Move the work item to `groupIdentifier` plus `basedOn` the Mendari Task.** The clean way to
  tell the two apart; a breaking change to `$decide`, kept for a future major release.
