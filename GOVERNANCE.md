# Governance

Mendari is an open-source project for healthcare decisions, so it is governed with two
aims: move openly, and never trade safety for speed.

## Roles

| Role | Who | Does |
|---|---|---|
| **Contributors** | Anyone who opens an issue or a pull request | Proposes changes |
| **Maintainers** | @Junebugg1214 (founder, lead maintainer) | Review and merge, cut releases, enforce the code of conduct, handle security reports |
| **Clinical reviewers** | Clinicians who volunteer, named in a pack's PR | Review a pack's criteria, risk level and failure modes before it is published as a reference pack |

A contributor becomes a maintainer after sustained, high-quality contributions and reviews, when
the existing maintainers agree.

## How decisions are made

- **Most changes:** a pull request, green CI, and one maintainer's approval.
- **Changes to the Decision Pack spec, a trust boundary or the risk policy:** an ADR in
  `docs/decisions/`, open for comment for at least a week, then decided by the maintainers.
  Silence counts as agreement, and an objection is answered before merging.
- **Safety first.** An objection on patient-safety grounds blocks a change until it is resolved:
  a missed critical case, automation beyond what the risk level allows, or a PHI exposure. It is
  not outvoted. The runtime's rules follow the same principle: policy may only lower
  automation, and level 4 is never automated (ADR-0002).
- **Disagreements** that review can't settle go to the maintainers. Their decision, and why, is
  written in the PR or the ADR.

## Reference packs and publishers

- A **reference pack** (`mendari/…`) needs green CI, including the eval gate, and a clinical
  reviewer's sign-off in the PR. Its Decision Card says honestly what it was and wasn't reviewed
  for.
- Publishers on the Hub are `community` by default. A publisher becomes **`verified`** when a
  maintainer has confirmed who they are. They become **`institutional`** when the publisher is
  a healthcare organization that stands behind its packs. The type is shown on every pack, and
  says who publishes it, not that the pack is clinically validated for your site: validate
  locally.

## Conduct and security

- The [code of conduct](CODE_OF_CONDUCT.md) applies everywhere in the project. Report concerns
  to the maintainers privately, through the repository's private reporting form (the
  **Security** tab, **Report a vulnerability**), titled "Conduct report"; see the code of
  conduct's "Enforcement".
- Security reports follow [SECURITY.md](SECURITY.md): the same private form, never a public
  issue.

## Changing this document

This document changes like the spec: by pull request, open for comment for a week, and
decided by the maintainers.
