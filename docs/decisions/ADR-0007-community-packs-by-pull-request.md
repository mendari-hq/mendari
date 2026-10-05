# ADR-0007: Community packs come by pull request, and where a pack lives is its label

## Status
Accepted (2026-10-04). The code, checks and docs are built; the GitHub settings (labels, the DCO
app, `pack-check` and DCO as required checks, the code-owner ruleset) and turning community
packs on for the hosted platform are the owner's steps (`tasks/todo.md`, B8 and B9).

## Context
The repository is going public so outside developers can contribute Decision Packs. Until now
every pack in the repository was one Mendari maintains (`packs/`), and the loader trusted what
it found:

- every `pack.yaml` under `packs/` was served, nested ones included, and when two packs shared
  an id the newest version won silently, so a pull request could shadow a reference pack;
- `publisher.type` is self-declared, and all five reference packs declare `community`, so the
  manifest cannot say who stands behind a pack;
- the risk level is self-declared too, and it decides whether a pack may automate;
- an unscoped provider setting (`router=jev`) reached any pack with that slot and variant, so a
  new pack could spend the deployment's remote calls;
- the PR controlled its own evaluation bar (`degradation_tolerance`) and what its records keep
  (`store_input`).

Clinical content from an unknown author, unreviewed by a clinician, must not act on its own,
spend money or claim a trust it has not earned, and none of the existing packs or deployments
may change because community packs exist.

## Decision
The owner decided (2026-10-04):

- **Location is the label.** Community packs live in `community-packs/<name>/`, one level deep,
  with the id `community/<name>`. Only the owner's merge puts a pack there. The origin of a
  pack (`core/origin.py`: maintained, community, hub, local) comes from where it was found,
  never from its manifest. A community pack uses the `community/` namespace only, so it never
  claims `mendari/` or `openhealth/`, nor a reference pack's id; a second claim to a community
  pack's id is refused and reported, whatever its version. Packs from anywhere else are found
  as before (the newest version of an id is served); one whose id is in `community/` gets the
  community limits wherever it was loaded from. A community pack's FHIR systems live under its
  own url (`https://mendari.co/packs/community/<name>/`), and its name, CDS title and publisher
  never name Mendari nor copy a maintained pack's name (CI).
- **Served, labelled, recommend-only.** A deployment serves community packs only with
  `MENDARI_COMMUNITY_PACKS=on` (default off; the one switch turns them all on or off). Every
  surface labels them "Community · not clinically reviewed" (the API, `/api/health` counts,
  the dashboard picker, the overview, Try it, the Decision Card, CDS Hooks discovery and cards,
  the `/docs` landing); the reference packs say "Maintained by Mendari"; `publisher.type` is
  shown only as self-declared. A community pack never automates: the policy caps AUTO to
  HUMAN_REVIEW (ADR-0002: policy only lowers automation), and no policy rule of its own
  blocks (refused at load; a block is final at once, so no person would confirm it). It reads
  at most 10,000 characters of text in all, the length its patterns are timed on.
- **Local providers only, unless the owner opts a pack in.** A community pack's default
  variants are local (`rules`, `criteria`, `dosing`, `naive_bayes`, or a composite of those);
  unscoped provider settings skip it, and only `community/<name>:slot=variant` runs it on
  something else. Its pinned `model` and `max_tokens` give way to the deployment's.
- **Risk level at most 1**, and `audit.store_input: hash`, refused at load otherwise.
- **The bar is the base branch's.** `mendari community-check` (the `pack-check` job, which runs
  on every pull request so it can be a required check) enforces scope, identity, manifest policy, tests, evaluation floors (critical detection at
  least 0.9, no critical case automated), no regression within a tolerance read from the base
  branch (at most 0.05), a benchmark, card and model CI regenerates and compares, a pattern
  check against slow regular expressions, size limits, version and CHANGELOG against the
  digest, and PHI warnings. Its limits are constants in the checker, which a community pull
  request may not change.
- **DCO.** Every commit is signed off (`git commit -s`); there is no CLA.
- **Governance.** The owner is the only merger, with a code owner's approval required. Each pack
  names its maintainers in `community-packs/MAINTAINERS.yaml`; a removed pack stays there as a
  tombstone, so its id is never reused. Clinical review is recorded in the pull request, not in
  the product.

## Consequences
- Nothing existing changes: the five reference packs decide identically, and with
  `MENDARI_COMMUNITY_PACKS` unset a deployment serves exactly what it served before (a pack
  pulled from a registry, a local copy of a reference pack, a path holding `packs/`), except
  that a pack whose id is in the new `community/` namespace gets the community limits. The
  community packs are not in the wheel or the images.
- An outside author sees the pack live (where community packs are on) with no power to act on
  a patient, spend the deployment's calls or look reviewed.
- The risk level is still self-declared: the reviewer is the backstop for under-declaring, and
  the level cap keeps level 2 and above (and the FDA clinical decision support question) off
  this route until a clinical reviewer group exists.
- A pattern that backtracks badly can still slip past the static check and the probe; Python's
  `re` has no time limit. Removal is the remedy: revert the merge, or turn the switch off.
- The owner is the bottleneck. The "Propose a pack" issue screens ideas before they are built,
  and CI catches the mechanical problems.
- A merged mistake with real patient data or licensed content cannot be fully taken back from a
  public history; the PHI warnings, the provenance review and the DCO reduce, not remove, that
  risk.

## Alternatives Considered
- **Community packs in `packs/`, labelled by `publisher.type`.** Rejected: the manifest is
  self-declared, the reference packs already declare `community`, and a sixth folder there
  changes the wheel, the images, the reference build and tests that count the packs.
- **Kept in the repository, served only from an owner's allow-list per pack.** Safest, but
  "available on the platform" becomes an extra owner step for every pack. The label and the
  recommend-only cap give most of its safety without it.
- **Served like maintained packs, automating by declared risk level.** Rejected: it trusts a
  self-declared level from an unknown author.
- **`<github-handle>/<name>` or organization namespaces.** Rejected for now: attribution without
  squatting, but handover is awkward and the id carries no trust signal; organizations need an
  approval step each.
- **A CLA, or nothing beyond Apache-2.0.** A CLA deters contributors and needs counsel; nothing
  leaves no record of the contributor's right to submit clinical content. The DCO is the light
  middle.
- **Uploads through the UI or API, and publishing from the registry.** Later, and only with
  isolation: each pack's rules in their own process with time and memory limits (or a regex
  engine with timeouts), so untrusted content never runs in the server's process. Until then
  the pull request is the only way in, and the owner reads everything that is merged.
