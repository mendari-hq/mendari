# ADR-0002: Automation limits are enforced by the runtime, and lifted only by the deployer

## Status
Accepted (v0.1)

## Context
A public pack could claim `AUTO` at any confidence. Healthcare risk depends on the intended use
(administrative routing vs. treatment recommendations), and the organization that deploys a
pack, not the publisher, is accountable for automating it at its site.

## Decision
- Each pack declares a risk level from 0 to 4. The runtime caps `AUTO` to `HUMAN_REVIEW` for
  levels 2–4, whatever the pack's thresholds say.
- Packs at level ≥ 2 may not even declare `AUTO` policy rules; validation rejects them.
- For levels 2–3, the *deploying organization* may pass a `GovernanceOverride` with a named
  approver and a reference (e.g. committee minutes). It is written into every affected decision
  and evaluation report. Level 4 can never be autonomous.
- Independently of risk level, any provider failure in a decision caps `AUTO`, because a
  failed safety screen means the safety checks are incomplete.

## Consequences
- Automation limits live in the runtime, not in each pack's own configuration, so a
  malicious or careless pack cannot remove them.
- Overrides are auditable, attributable and local, matching how clinical AI governance works
  in practice (AI or clinical decision support committees).
- Some legitimate level 2 automation requires an explicit governance step. That is
  intentional.

## Alternatives Considered
- **Trust pack thresholds.** Rejected: it puts the publisher, not the deployer, in charge of
  automation at someone else's site.
- **Hard-block level 2–3 automation entirely.** Rejected: validated automation of some
  clinical-support tasks can be appropriate, but it must be an explicit, recorded local
  decision.
