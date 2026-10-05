# Mendari: task tracking

Roadmap to the full goal: [`docs/roadmap.md`](../docs/roadmap.md).

## Plan: community packs through pull requests (2026-10-04)
Make the repository public so outside developers can contribute packs by pull request. Plan from
three research reports on `b00e2809` (public repo, contribution flow, shipping and the bar).
**Owner decisions (2026-10-04):** community packs live in `community-packs/<name>/` with ids
`community/<name>`; they are served live but **recommend only** (never AUTO, local providers
only); risk level **≤ 1**; contributions are signed off under the **DCO** (`git commit -s`, the
DCO app); the owner's gmail address on five old commits on `main` (and PR refs #1–7) is accepted,
and GitHub's email push protection goes on. Location is the trust label, never the manifest.

**Part A: before the repository goes public.** Build items on branch `claude/public-prep`.
- [ ] **A1 (owner)** Accept the gmail address in history; turn on "Block command line pushes
  that expose my email" (owner steps below).
- [x] **A2** The sdist holds only `src/` (with the vendored docs UI), `packs/`, `datasets/`,
  README, LICENSE and pyproject (`[tool.hatch.build.targets.sdist]`; the tests read repository
  files the sdist leaves out, so they run from a clone); the README's links are absolute, so
  they work on pypi.org;
  `scripts/check_sdist.py` fails on anything else, in CI (`hub-e2e`, which now installs the wheel
  built from the sdist) and in `release.yml`; `tests/test_packaging.py`.
- [x] **A3** Internal notes (the milestone plans, the dashboard design review and grade, the FHIR
  and docs audit, the overnight log) moved from `docs/` to `notes/`, outside the docs site, the
  sdist, the image and the function bundle; links updated. Production specifics scrubbed from the
  tree (host, curl check, env record id, project, database and connector names, region,
  registrar, a private artifact URL, the audit's production values, the deployed provider
  mix, model pins and daily limits, the access setting's history). History keeps them;
  `tests/test_public_tree.py` keeps them out of the tree.
- [x] **A4 (build part)** Security and conduct reports go through GitHub private vulnerability
  reporting (SECURITY.md, CODE_OF_CONDUCT.md, GOVERNANCE.md, the issue chooser). No e-mail
  address is published. Turning the form on is A10.
- [x] **A5** `packs.yml` takes pack directories NUL-separated (`scripts/changed_packs.py`, `git
  diff -z`), nested packs included, and passes them by argv only; `.github/CODEOWNERS`;
  `images.yml` read-only by default, `packages: write` and a `ghcr` environment on its job; a
  `secrets` job in `ci.yml` (gitleaks v8.30.1 pinned by digest, full history, one allowance for
  `deploy/onprem/.env.example`); `tests/test_workflow_safety.py` holds the fork-PR rules.
- [x] **A6** README "Install" (from a clone until 0.1.0 is on PyPI; the `server` extra;
  `MENDARI_PACKS_PATH` and `MENDARI_PACKS`); no `pip install mendari && mendari pull` promise;
  `mendari new` prints the guide's URL; example image tags `v0.1.0` (the package version).
- [ ] **A7 (owner)** Repository homepage and description.
- [ ] **A8 (owner, Vercel)** Fork protection kept, Preview env cleanup, bypass secret, Neon
  preview branches.
- [ ] **A9 (owner)** Make the repository public.
- [ ] **A10 (owner)** Private vulnerability reporting, secret scanning with push protection,
  Dependabot, CodeQL, fork-PR approval for first-time contributors.
- [ ] **A11 (owner, then build)** Protected `pypi` and `ghcr` environments, a `v*` tag ruleset,
  the PyPI trusted publisher; then 0.1.0 and the docs on GitHub Pages (a deploy workflow is a
  later PR), and the README's install line switches to `pip install mendari`.

**Part B: the contribution flow** (a later PR; B1–B5 before B9; order B1 → B2 → B4 → B5 → B3 →
B6/B7 → B8 → B9 → B10).
- [x] **B1 Origin-aware loading.** `search_paths()` returns (path, origin): `maintained`,
  `community`, `hub`, `local`. `community-packs/` is searched only with
  `MENDARI_COMMUNITY_PACKS=on` (default off: the wheel and self-hosters are unaffected). A
  community pack sits exactly at `community-packs/<name>/pack.yaml` with id `community/<name>`;
  anything else is `invalid`. A taken id never replaces another pack, whatever its version: both
  are reported and the maintained one kept. `mendari/` and `openhealth/` are reserved to
  `packs/`, and `community/` to `community-packs/`. Tests in `tests/test_community_packs.py`.
  Built: `core/origin.py` (origins and community rules), `Pack.origin`, `SearchPath` in
  `core/registry.py`; the duplicate rule applies to every origin (before, the newest version
  won silently); the shipped packs decide identically (7,484 decisions compared with main).
- [x] **B2 Policy and spend.** A community pack is capped at HUMAN_REVIEW in `core/policy.py`
  (recommend only; ADR-0002: policy only lowers automation). Unscoped provider settings skip
  community packs; only `community/<name>:slot=variant` applies. A community pack's pinned
  `model` and `max_tokens` give way to the deployment's. Refused at load: risk level above 1,
  `store_input` other than `hash`, a default variant that is not local (`rules`, `criteria`,
  `dosing`, `naive_bayes`, or a composite of those).
- [ ] **B3 Labels everywhere.** "Community · not clinically reviewed" by origin on `/api/packs`,
  `/api/packs/{id}`, `/api/health` (packs by origin), the dashboard picker and card header, Try
  it, the Decision Card, CDS Hooks discovery and each card's `source.label`. The dashboard's
  default pack is the first maintained one. `publisher.type` shown as self-declared. Tokens only;
  contrast tests extended.
- [x] **B4 `mendari community-check`** (and `scripts/community_check.py`, shared by the CLI and
  CI): scope, identity, manifest policy (`license: Apache-2.0`, `publisher.type: community`,
  `store_input: hash`, local providers, no pinned model, `degradation_tolerance` ≤ 0.05, risk
  level ≤ 1), correctness, evaluation floors (critical detection ≥ 0.9, no critical case
  automated, ≥ 5 benchmark cases per critical label), the benchmark and card regenerated and
  compared, a retrained model byte-identical, the regex probe, size caps (2 MB, 200 files),
  version and CHANGELOG against the digest, PHI heuristics as warnings. A unit test per rule.
  Built: `src/mendari/community/` (`check.py` the rules, `regex_safety.py` the static check and
  the subprocess probe, `phi_scan.py`, `maintainers.py`, `gitbase.py`), tests in
  `tests/test_community_check.py`. Also refused: symbolic links, files in `evals/` other than
  the benchmark (a leaderboard's numbers cannot be regenerated), a pack with no critical label,
  a pack without a `MAINTAINERS.yaml` entry. With a remote default provider the tests and the
  evaluation do not run (they would call the service).
- [x] **B5 CI.** One always-running, required `pack-check` job (exits 0 with "no pack changes"),
  pack directories from `git diff -z`, covering `packs/` (the existing gate) and
  `community-packs/`; a PR that touches a community pack and anything outside one
  `community-packs/<name>/` fails unless the owner wrote it. `ci.yml` runs `mendari test` on
  every community pack. No `pull_request_target`, no secrets.
  Built: `packs.yml` job `pack-check`; `scripts/changed_packs.py` covers `community-packs/`;
  `scripts/community_check.py` (scope, each changed pack, a removed pack's tombstone).
  "Owner" is `github.actor` equal to `github.repository_owner`.
- [x] **B6 Scaffold.** `mendari new --community` writes the community defaults, and the pack's
  benchmark and card, so it passes `community-check` as written (once listed in
  `MAINTAINERS.yaml`).
- [ ] **B7 Docs and templates.** `docs/guides/contributing-a-pack.md` (the flow, synthetic data
  without Python, `ABSTAIN` carries no recommendation, a risk-level table with the inbox router
  as the level-0 precedent, a pattern guide); `.github/PULL_REQUEST_TEMPLATE/pack.md`; a
  "Propose a pack" issue form; CONTRIBUTING on the DCO and content provenance;
  `community-packs/README.md` and `MAINTAINERS.yaml` (removed ids stay as tombstones; both
  written with B4).
- [ ] **B8 (owner)** Labels (`pack`, `pack:new`, `pack:update`, `needs-clinical-review`,
  `clinical-review:provided`, `risk-level-change`, `patient-safety`, `idea`, `good first pack`);
  the DCO app; `pack-check` and `DCO` required; a `main` ruleset requiring a code owner's
  approval, with the admin on the bypass list.
- [ ] **B9 (owner)** `MENDARI_COMMUNITY_PACKS=on` in Vercel Production only; on the owner's own
  branch preview (never a fork's), check that `community-packs/` reaches the function bundle.
- [ ] **B10 (owner and build)** Dry run from a second GitHub account, including a bad version
  (a ReDoS pattern, a shadowing id, an edit outside its folder, a faked benchmark) that CI must
  refuse. Then announce.

### Going public: owner steps
In this order; each step says where to click and what to check after. Nothing here is done by a
build session. A1–A8 come before A9 (making the repository public); A10 and A11 right after it.

1. **Merge the public-prep PR** (`claude/public-prep`) once CI is green. Check after: on `main`,
   `docs/` has no plans, reviews or logs (they are in `notes/`), and the `secrets` job ran green.
   Optionally make `secrets` a required check: Settings → Branches → the `main` rule → Require
   status checks → add `secrets`.
2. **A1, email push protection.** github.com → your avatar → Settings → Emails: keep "Keep my
   email addresses private" on and tick **"Block command line pushes that expose my email"**.
   Check after: `git config user.email` in your clones is the `…@users.noreply.github.com`
   address; a push of a commit authored with the gmail address is refused (GH007). The address
   on the five old commits stays (decided 2026-10-04).
3. **A7, homepage and description.** The repository page → the gear next to **About**: Website
   `https://mendari.co`; reword the description (for example "Open, auditable Decision Packs
   for healthcare decisions"); add topics if you like. Check after: the About box shows
   mendari.co, not a vercel.app host.
4. **A8, Vercel** (the engine's project):
   - Settings → Git: **Git Fork Protection** stays on. Rule from now on: never click
     "Authorize" on a deployment for a fork's pull request; pack PRs get CI, not previews.
   - Settings → Environment Variables: for `JEV_CONNECTOR`, `JEV_API_URL`, `JEV_MODEL`,
     `LLM_MODEL`, `MENDARI_JEV_DAILY_LIMIT` and `MENDARI_LLM_DAILY_LIMIT`, edit each and untick
     **Preview** (Production only). Check after: a new preview of your own branch reports no
     Jev or LLM configuration in `/api/health` (signed in) and still deploys.
   - Settings → Deployment Protection → **Protection Bypass for Automation**: if the secret was
     ever pasted anywhere (a chat, a script, a PR), regenerate it; delete it if nothing uses it.
   - Storage → the Neon database → its settings: preview deployments get their own database
     branch. Check after: the Neon console's Branches list shows `preview/<your branch>` for a
     fresh preview, and the `neon-preview-cleanup` workflow deletes it when the PR closes.
5. **A9, make it public.** Settings → General → Danger Zone → **Change repository visibility**
   → Make public. Check after: in a private browser window, the repository, its Actions runs
   and its PRs open without signing in; `.github/ISSUE_TEMPLATE` shows its two private links.
6. **A10, right after the flip.** Settings → **Code security** (named "Code security and
   analysis" or "Advanced Security" on some accounts):
   - **Private vulnerability reporting**: Enable. Check after: the Security tab shows **Report
     a vulnerability** (from a second account or signed out), and the links in SECURITY.md,
     CODE_OF_CONDUCT.md and the issue chooser open the form.
   - **Dependency graph** on; **Dependabot alerts** and **Dependabot security updates**: Enable.
     Check after: Security → Dependabot lists the alerts (or none).
   - **Secret scanning**: Enable, and **Push protection**: Enable. Check after: Security →
     Secret scanning shows no open alert (gitleaks found none beyond the allowed example file).
   - **Code scanning → CodeQL analysis → Set up → Default**. Check after: the first CodeQL run
     in Actions finishes, and Security → Code scanning lists its results.
   - Settings → **Actions → General**: "Approval for running fork pull request workflows from
     contributors" → **Require approval for first-time contributors**; "Workflow permissions" →
     **Read repository contents and packages permissions**, and "Allow GitHub Actions to create
     and approve pull requests" unticked. Check after: a PR from a second account waits on
     **Approve and run** the first time.
7. **A11, before any tag.**
   - Settings → **Environments** → New environment `pypi`: Required reviewers: you; Deployment
     branches and tags → Selected branches and tags → add a **tag** rule `v*`. Then the same for
     `ghcr` (`images.yml` uses it). Check after: both list one reviewer and the `v*` tag rule.
     (On GitHub Free, protection rules work only once the repository is public.)
   - Settings → **Rules → Rulesets** → New ruleset → **New tag ruleset**: name "release tags",
     Enforcement Active, Bypass list: Repository admin; Target tags: include `v*`; rules:
     **Restrict creations**, **Restrict updates**, **Restrict deletions**. Check after: the
     ruleset shows Active with those three rules.
   - pypi.org → Your account → **Publishing** → Add a new pending publisher (GitHub): PyPI
     project name `mendari`, owner `Junebugg1214`, repository `mendari`, workflow
     `release.yml`, environment `pypi`. Check after: it is listed as a pending publisher, and
     the name `mendari` is still free on PyPI.
8. **Release 0.1.0.** On an up-to-date `main`: `git tag v0.1.0 && git push origin v0.1.0` (the
   tag must equal the package version; `release.yml` checks). In the run, approve the `pypi`
   deployment and the six `ghcr` deployments. Check after: pypi.org/project/mendari shows 0.1.0
   with a wheel and an sdist; in a fresh venv, `pip install mendari` then `mendari test
   mendari/patient-inbox-router` passes; the release job's "The sdist holds only what a source
   build needs" step passed; GitHub → your profile → Packages lists the six `mendari-*`
   images (make each public in its package settings if it is private).
9. **Docs on GitHub Pages** (a build PR adds the deploy workflow first). Settings → **Pages** →
   Source: **GitHub Actions**. Check after: the site opens at the Pages URL, then a build PR
   points the README and `mendari new` at it and switches the install line to `pip install
   mendari`.

## Built: pack content safety (2026-10-04)
From the launch-readiness review, before any registry, Hub or upload feature lets an outside
author's pack run on a shared deployment. Branch `claude/pack-safety-core`. No pack, data, model
or benchmark change; the five shipped packs keep their digests and decide their examples
identically.
- [x] **1. A pack may not choose secrets or endpoints (E8)**: the eight `*_env` keys of `jev`
  and `llm` variants removed; the variable names are fixed constants, and a variant (or a
  composite's member) that sets one makes the pack invalid at load (`core/spec.py`), naming the
  variant and keys, never values; the provider refuses such a config too.
- [x] **2. Nothing in a pack reaches outside it (E9, D6)**: archive names split as written
  (`pack/` and plain segments; `pack//<absolute path>` was written at that path); `extract`
  checks every target is inside the destination; only regular files (no sparse member); the
  archive read as a stream with caps on members, member size, total size and expansion ratio
  checked as it expands. `pull` validates the registry's namespace, name and version and confines
  the cache path before any write, and the signed manifest must name that id and version (T11).
  The input schema is self-contained (local references that resolve, no nested `$schema` or
  `$id`, no `$dynamicAnchor`, no `format: regex`) and its validator retrieves nothing. A
  directory pack with a symbolic link out of it is refused (hidden entries and `__pycache__`, never
  part of a pack, are skipped: a `.venv` beside it is fine, as on main); fixed-name
  reads go through `Pack.path`; `mendari card --write` does not write through a link.
- [x] **3. A pack never stops the server (D7)**: any error loading a pack or building its
  terminology lists it under `/api/health` `invalid` (by type when not Mendari's) and the others
  are served; a served pack whose providers cannot be built is listed there and answers `422`,
  never failing `/api/health` (T9); providers stay lazy, built at first use as on main. YAML is
  bounded on the parsed graph of a file that uses anchors (1,000,000 nodes and 32,000,000
  characters of scalar text, aliases expanded, each node visited once; no alias loop) before
  anything walks it (T2, T7); YAML or JSON too deep, not UTF-8, a YAML value Python cannot build,
  or a schema the validator cannot read is a `PackValidationError`. A version has at most 64 characters and
  `version_key` never calls `int()` (T10). A registry publish answers `422`, never `500`, for a
  pack that does not load or whose card is not text; a malformed benchmark is shown as none,
  only typed scalars and a leaderboard of at most 32 levels and 1 MB are kept, and the metadata
  is serialized before it is stored (T12).
- [x] **4. Pack patterns compiled at load (residual risk in the threat model)**: every rules
  pattern (every variant and composite member, case ignored), input-schema `pattern` and
  `patternProperties` key is
  compiled with `re` at load, at most 2,000 characters, at most 500 per pack counted before any
  is compiled; a failure is a `PackValidationError` naming pack, file and rule. Patterns run on
  `re` as on main, with no time limit; Mendari's own dosing and CQL patterns are unchanged. The
  rules files read at load are kept on the `Pack`, so a provider does not read them again.
- [x] **5. CQL libraries namespaced per pack (T4)**: a pack's library goes up on the shared
  engine under a resource id and a declared name ending in hashes of the pack id and the text
  (the name rewritten in its declaration, which must be the first statement), so an outside
  pack declaring the same name and version neither replaces nor shadows it. Main's `_HEADER`
  search is unchanged; evidence keeps the declared name.
- [x] **6. Docs, exactly true**: SECURITY.md (installing a pack means trusting its author;
  patterns on `re` with no time limit can slow or stall a worker; packs from others isolated
  once uploads or a public registry exist), threat model (E8, E9, D6, D7, T4 and a residual-risk
  entry for patterns), spec sections 3, 5 and 13, the authoring guide, README.
- [x] **7. What a pack decides with is in its digest; YAML errors clean; reference examples
  bounded**: `Pack.path` and `load_pack` refuse a path through a hidden entry or `__pycache__`
  (as written or with links resolved), and `load_pack` a manifest path into `evals/` or a
  `DECISION_CARD.md`, naming the field (`config: .logic/triage_rules.yaml` loaded and decided
  outside the digest a governance approval is bound to). Any error parsing or building a pack
  YAML file (a tagged `!!bool maybe` raised `KeyError`) is a `PackValidationError` naming the
  file and the error's type, never the value. The expansion cap weighs scalar text at its
  JSON-escaped length, and the API reference includes a pack's example only when it is at most
  64 KB as JSON (a 2 MB tests file of `"\0"` made `/openapi.json` 180 MB).
- [x] **8. A pack reads only digested files; YAML numbers bounded (E9, D7)**: the refusals in
  7 compared spellings, so on a case-insensitive filesystem `EVALS/t.yaml`, `__PYCACHE__/t.yaml`
  or `logic/decision_card.md` read a file the digest skips, and a `cql` `library:` was not
  checked at load (it could live in `evals/` anywhere). `load_pack` now hashes first and keeps
  the hashed files' identities (device, inode); `Pack.path`, and every read at load, accepts an
  existing file only if it is one of them, whatever spells it. The written-path checks stay for
  their messages, naming the field (`library` added). Attestations are read through
  `Pack.attestation` only, and `evaluation.benchmark` must be in `evals/`. Pack YAML loads with
  a `SafeLoader` subclass that refuses a number of more than 4,300 characters (underscores
  removed) or 4 `:` parts before building it, and an integer of more than 4,300 digits, in every
  base (a 480 KB base-60 integer took 23.6 s). A behaviour test that is not JSON (a lone
  surrogate, `!!binary`, a date, a set, NaN) is a validation problem named by type:
  `mendari validate` reports it, `GET …/examples` answers `422` (was a `500`). A
  `ConstructorError` is named by type, line and column, never its text; a parse error keeps the
  parser's message, as the docs now say. Spec §3, §10, §11, §13, SECURITY.md, threat model E9
  and D7, authoring guide.
- [x] **9. Malformed pack content is named, never a 500 (D7, T2, E9)**: the review's eight
  remaining findings. (1) A behaviour test must be a mapping with an `input` mapping, an
  optional text `name` and an optional `expect` mapping (`answers` a mapping, `outcome_in` a
  list), named by case number; the API reference picks each pack's example under its own guard
  (`expect: [1]` made `/openapi.json` a `500` for every pack). (2) The YAML loader refuses a
  string with a lone surrogate, `!!binary`, `!!set`, `!!omap` and `!!pairs` by file, line and
  column, and every pack JSON file is walked for lone surrogates and non-finite numbers (a
  surrogate in a pack's name made `GET /api/packs` a `500`). (3) `Pack.read_json`,
  `read_text`, the naive_bayes model's shape and the digest's reads name the file (no traceback
  from `mendari validate` or `test`). (4) Content verified at read: the load keeps each hashed
  file's SHA-256, and every read (`Pack.read_bytes`) checks and parses the bytes it read, so a
  file edited in place after the load is refused, not used under the old digest. (5)
  `evaluation.benchmark` must be in `evals/`; a malformed benchmark or leaderboard is absent
  (the card says why), a non-UTF-8 `CHANGELOG.md` a `422`. (6) Every value the YAML constructor
  cannot build carries its line and column. (7) A bad dataset line is named by file and line.
  (8) An archive whose names are one file under case-folding and NFC is refused by `verify`;
  `extract` names a file it cannot write.
- [x] **10. The focused check's three remaining lows**: pack JSON refuses an integer too large to
  be a number (a 400-digit accuracy in a benchmark or leaderboard, or a model's temperature,
  overflowed into a `500` or a traceback); an LLM calibration's `temperatures` must be a mapping;
  `compare` reports a published benchmark whose figures are malformed as `unknown` with the
  reason, never a `500` from `POST …/evals`.
- Verified: the five packs decide every dataset record and `tests/cases.yaml` example (default
  providers and every local variant combination, 9,958 decisions) identically to `origin/main`
  run on its own source, in one long process, in a fresh process per pack and variant set, and
  in a fresh process per example; process start plus first decision is within 5% of main.
- Deferred: untrusted packs run in an isolated process with memory and time limits, with the
  upload or registry feature; the regex-engine approach was tried and dropped: see the review
  notes (`claude/pack-safety`, its round-2 and round-3 reviews: the engine could not match
  `re`'s semantics exactly, its clock was the process's, and its tables slowed every start).

## Built: the SMART launch gated, C5 and S5 (2026-10-03)
From the Phase 4b review's residual risks, after research (three researchers and a critic). The
owner chose: the EHR's sign-in identifies the clinician; a launch may view or decide per
registered EHR; no iframe and no refresh until the pilot EHR is known
([ADR-0006](../docs/decisions/ADR-0006-an-ehr-launch-is-a-principal.md)). Branch
`claude/smart-gating`. About 10 days. No schema change.
- [x] **1. Seal the launch rows (S5)**: session and pending-launch rows sealed (AES-256-GCM, key
  derived from the row's own cookie, bound to row and kind); a row that will not open ends the
  launch; an EHR 401 ends it; token syntax checked; `offline_access` refused
  (Deviation: a take of a sealed row that will not open raises `SealError` rather than reading
  as absent, so the callback can tell `browser` from `state_unknown`; the row is deleted alike.)
- [x] **2. The launch is a principal (C5)**: `MENDARI_SMART_EHRS` registration (FHIR base to
  organization and `view` or `decide`); with sign-in on, unregistered bases, missing
  `openid fhirUser`, and patient or related-person users refused at launch; `ehr` principals with
  two permissions of their own; a bearer header decides alone on `/api/smart/*`; signing out ends
  the launch; limits per principal
  (Deviation: a person's session alone on a launch route is a `401`, not a `403`: the launch
  cookie is the only credential there. Signed-in `/api/health` lists only the caller's
  organization's registrations, every one in open mode.)
- [x] **3. The chain names who asked**: `requested_by` on new `decision.created` events for every
  principal with sign-in on; the FHIR requestor agent; validator sample
  (Deviation: on the Provenance the requester is a second agent with the role "requester" as
  text and no type, since R4's agent types have no code for one; the HL7 validator accepts it
  with no new warning.)
- [x] **4. The dashboard for a launch**: patient view only, decision inline, "open Mendari again
  from the EHR" after a 401, a replaced launch said so
  (Deviation: the launch pin is checked again before every render and every decision, and a
  window opened on `#patient` with no pin takes the launch it shows; the 401 message applies to
  signed-in people too, who were told to sign in again.)
- [x] **5. CI and wrap-up**: `sandbox-e2e` with sign-in on and a registered EHR, `mendari smart
  check`, threat model, SECURITY.md, deploying-with-phi.md, review packet addendum, audit status
  (Deviation: `--expect-org` reads the organization and `requested_by` from the engine's own
  store (`--db`, run in its container), not over HTTP, since the profile's identity provider is
  a placeholder nobody signs in through; `smart check` also reports the SMART client and scopes,
  which refuse launches too.)
- [x] **Review**: four reviewers and a skeptic; 8 findings, 4 confirmed and fixed (replaced
  launches end, open-mode launches end at sign-in on, the pin checked by the server, `smart
  check` fails an organization nobody signs in to), and "read on every request" corrected
- Deferred: iframe mode and refresh (when the pilot EHR is known), sealing sign-in rows (with a
  PHI keyring), token revocation at logout, a registration table, one FHIR base for several
  organizations

## Built: hardening before PHI, Phase 4b (2026-10-02)
From the [audit](../notes/fhir-and-docs-audit.md) Phase 4b line, after research (four researchers
and a critic). The owner chose finite spending ceilings plus per-organization quotas at launch,
verified emails for people in the chain, and the pilot site's own servers for the first PHI
([ADR-0005](../docs/decisions/ADR-0005-hardening-before-phi.md)). Built in three lanes
(`claude/phase-4b` spine, `claude/phase-4b-web`, `claude/phase-4b-notes`), then integrated.
About 18 days of work.
- [x] **A. Public surface**: the capability statement reads its version without the chain lock;
  OIDC discovery cached an hour per issuer; expired session rows purged 100 at a time on an
  index; public `/api/health` is `{status, version, sign_in}` (details need `read`); a 4 MiB body
  cap (`MENDARI_MAX_BODY_BYTES`, `413`); `auth.check` off the event loop; `Principal.id`
  - Deviations: the cap counts every method and sits innermost (Starlette's middleware turns a
    mid-body 413 into a 400), 64 KiB at least; sessions now store `iss`, and an id_token
    without `sub` is refused
- [x] **B. Supply chain**: actions pinned by SHA with `persist-credentials: false`, images by
  digest, the Docker image installed from `uv.lock`, `.github/dependabot.yml` monthly with no
  previews for its branches, `tests/test_supply_chain.py`
  (Deviation: the docs package's npm entry comes with stage C, which adds the package.)
- [x] **C. Docs page safety**: Swagger UI vendored at a pinned version with license and checksums
  at `/docs/reference`; `/docs` and `/redoc` redirect there until Phase 5; a CSP and COEP on every
  response; bearer schemes for keys and tokens in the OpenAPI
  (Deviation: 5.33.0, not 5.33.1, which was a day old against the 7-day cooldown; the bundle's
  third-party license file ships too.)
- [x] **D. Rate limits and quotas**: a GCRA limiter in the database per principal on costly routes and
  per IP on sign-in and the token endpoint (`MENDARI_RATE_LIMITS` on/log/off), `429` with
  `Retry-After`; per-organization daily decision quotas (`MENDARI_ORG_DAILY_LIMITS`, evaluations
  not counted); Vercel firewall rules as a file and runbook; proxy headers for self-hosted
  - Deviations: overrides follow the mode in the one variable; sign-in counts `/auth/login` and
    the SMART launch (callbacks left to the edge), SMART decide by a digest of its session;
    token failures count `invalid_client` only; one firewall rule (`/auth/`, `/smart/`, 120/min)
- [x] **E. Security log**: JSON lines on stdout from an allow-list of fields, a request id, one
  access line per API request, security events; no credential, query value, body or case text;
  no exception messages under `hash` or `encrypt`
  - Deviations: `count`, `error` and `frames` join the allow-list; FHIR errors carry the id in
    the header only; the `caplog` test reads stdout now
- [x] **F. Sessions and identity**: 30 minutes idle, 8 hours at most, roles recomputed per
  request, `__Host-` names with the old name accepted for one release, sign-in and SMART launch
  tied to the browser; people chained by verified email, keys as `key <id>`
  - Deviations: a SMART session also ends with the EHR's token; `next` with a backslash or a
    control character goes to `/`; health shows `sessions`; open-mode outcomes and revocations
    stay `open mode`; the dashboard shows a banner and never navigates away on its own
- [x] **G. Origin checks**: a cross-site or same-site browser request reaches only public and page
  routes, only to read; HSTS
  (Deviation: without Fetch Metadata a foreign or `null` Origin limits GET too, not only unsafe
  methods, and an unknown `Sec-Fetch-Site` counts as cross-site: both fail closed.)
- [x] **H. Notes out of the chain**: a `notes` table (sealed under `encrypt`, refused under
  `hash`), a salted commitment in the chain, `note.erased`, PAS payer text sealed, batched
  `input.read` writes; old chained notes hidden from readers without `read_input`
  (governance `reference` stays chained, as the critic's stage H left it out)
- [x] **I. Record hygiene and keyed hash**: evidence dropped under `hash` and sealed under
  `encrypt`; Patient ids in issues stored as type and position; `MENDARI_INPUT_HASH_KEY` HMAC
  required in `hash` and `encrypt`
  (issue labels are positional in every mode, not only PHI ones; the two keys must differ)
- [x] **Integration**: web and notes lanes merged into the spine; origin refusals logged
  (`auth.origin.refused`) with a request id; the log's `phi.inputs.read` also counts opened notes,
  as the chain does; other lanes' tests updated for request ids, chained emails and the input
  hash key; `run_async` for tests sharing a process with Playwright
- [x] **K. Wrap-up**: threat model ids made unique and in order (the logs row is I10), SECURITY.md
  in one order, one go-live checklist with the launch order and a settings table in
  `deploying-with-phi.md`, README, ADR-0005, the audit's "Status after Phase 4b", the packet
  for the external review (`docs/security/review-packet.md`)
- [x] **Review**: five reviewers and a skeptic; 12 findings confirmed (3 medium, 9 low; two found
  twice), all fixed: CDS order charge, compose proxy trust, sign-in sizing, the daily-limit doc,
  sign-in on aliases, cookieless logout CSRF, library URL logs, SMART launch log, payer codes
  only, `decide --db` under the PHI mode
- At launch (owner), after the external review: Jev 2,000 and LLM 200 a day, 5,000 decisions per organization a day, the
  firewall rules enforced after 7 days in log mode, then Vercel Authentication lifted
- Deferred: a PHI keyring and `rekey`, database tables for security events, limits on reads,
  sealing the EHR token and a Mendari principal for `/api/smart/decide` (before the SMART launch
  is turned on with sign-in)
- Next release: stop reading the old cookie names (`mendari_session`, `mendari_smart`) on hosts
  that use `__Host-` names (`server/sessions.py`)

## Plan: docs experience, Phase 5 (2026-10-02)
From the audit's "Redesign plan". Branch `claude/phase-5`, after Phase 4b's stages C and G.
- [x] **L. Snippets checked in CI**: the quickstart, FHIR and authentication calls as scripts run
  against a local server in CI, open mode and with a key; README curl blocks equal to them
  (Deviation: the FHIR body is the API reference's synthetic Bundle, `api_docs.FHIR_EXAMPLE`; the
  packs ship no FHIR examples. The key pass skips steps by the route rules, not by a marker.)
- [x] **M. Landing at `/docs`**: public and static: what Mendari is, the four outcomes, a diagram,
  a three-call quickstart with copy buttons, FHIR in and out, authentication generated from the
  route rules, links to the reference and Decision Cards; light and dark from the dashboard's
  tokens, contrast-tested, keyboard and screen-reader checked, no CSP exemption
  (Deviation: each pack's Decision Card is linked in open mode only; with sign-in on, one link to
  the dashboard's cards, since which packs run is not public. The table lists every mounted route
  under `permission_for`. Each page's import map now holds only the modules its scripts reach.)
- [x] **N. Guides and links**: `docs/guides/api.md` in the mkdocs site from the same snippets;
  README and the OpenAPI description point to `/docs`
  (Deviation: the guide includes each script whole, not step by step.)
- [x] **Brought onto Phase 4b**: `claude/phase-4b` merged in; the snippets pass under its rate
  limits with a key (the check hides the server's security log unless `--show`); the landing's
  authentication section describes sessions that end, `429` with `Retry-After` and requests from
  other sites, with defaults only (a deployment's own session lengths and rates stay behind
  `read`, as stage A keeps them)
- Deferred: a public demo deployment (a launch decision), theming the mkdocs site

## Built: authentication and authorization, Phase 4 (2026-10-02)
From the [audit](../notes/fhir-and-docs-audit.md) Phase 4, after research (three researchers and a
security-minded critic). The owner chose the token endpoint plus scoped keys, a 30-day expiry for
existing keys, and URI scopes ([ADR-0004](../docs/decisions/ADR-0004-machine-access-tokens-and-scopes.md)).
Branch `claude/fhir-phase-4`. About 6.5–7 days.
- [x] **Session table** (a flaw the critic confirmed in production code): public logout deletes
  rows of any kind by a key the caller chooses. Deletes are scoped to a kind, keys are
  domain-separated digests, reads and takes are non-exclusive, and `smart_claim` gives
  one-time use
- [x] **Bearer handling**: an `Authorization` header decides alone (no cookie fallback, scheme
  case-insensitive); the prefix decides key (`ohk_`) or token (`mat_`), anything else is a 401;
  no credentials in URLs or forms; `WWW-Authenticate` on 401/403; a people-only `keys` permission
  for `/api/auth/(keys|clients)`; audit verify and FHIR export need admin; `no-store` on `/api`
- [x] **One scope language** (`server/scopes.py`): SMART v2 `system/` scopes for what is served,
  the v1 mapping, the decide and outcome URIs; effective = role ∩ scopes; strict for keys,
  requested ∩ registered for tokens; a route × principal × role × scope matrix test
- [x] **Scoped keys**: `scopes`, `expires_at`, `revoked_by` (additive); 90 days by default, 365
  at most; new keys `viewer` or `service`; existing keys expire 30 days after deploy; create and
  revoke chained
- [x] **SMART Backend Services** (`MENDARI_BACKEND_SERVICES`, off by default; refuses open mode
  and a missing public URL): `POST /auth/token`, RS384/ES384 assertions checked before any write,
  jti once per client, opaque five-minute `mat_` tokens with authorization read per request,
  clients with a JWKS by value, people-only and chained; Mendari's Bulk client as the end-to-end
  test
- [x] **Discovery**: `/fhir/.well-known/smart-configuration` only when on (`application/json`);
  the CapabilityStatement's security says SMART only when on
- [x] **Outcome integrity**: `source` a system identifier, `recorded_by` the principal,
  `final_label` one of the labels
- [x] **Docs**: ADR-0004, threat model, SECURITY.md, deploying-with-phi.md, API docs, README, the
  audit's status
- Phase 4b, before Vercel Authentication is lifted or any PHI: notes out of the chain (encrypted
  side table), a keyed input hash, an access and security log, rate limits and per-organization
  quotas, session idle timeout and `__Host-` cookie, Origin checks, Dependabot and pinned actions
- Deferred until asked: external authorization servers, JWKS URLs, pack-scoped keys, rotation,
  last-used tracking, a new key format, the dashboard Keys view

## Built: FHIR search, Phase 3 (2026-10-02)
From the [audit](../notes/fhir-and-docs-audit.md) Phase 3, after research (three researchers and a
critic). The owner chose a Task per decision ([ADR-0003](../docs/decisions/ADR-0003-a-served-task-is-a-decision.md)).
Branch `claude/fhir-phase-3`. About 4–5 days.
- [x] **Task per decision** (`standards/task_fhir.py`): derived on read from the decision row
  and its chain events, nothing new stored. `requested` (waiting for a Mendari reviewer),
  `completed`, `failed` (refused input: one column-only predicate shared by rendering and SQL);
  outputs from event payloads (`record_outcome` overwrites `final_label`); versions = the
  version-making events (created, review resolved, outcome recorded); no `for`, `focus` or
  notes beyond reasons; reasons left out for refused inputs (older records may quote input)
- [x] **Read and vread**: `GET /fhir/Task/{id}`, `/_history/{vid}`; `GET /fhir/AuditEvent/{id}`,
  `/fhir/Provenance/{id}`; ETag `W/"n"`, no conditional read; 404 across organizations
- [x] **Search core** (`standards/search.py`): one table of supported parameters drives the
  parser, the SQL filters, the CapabilityStatement and the tests. GET and POST `_search`
  (form body: R4 SHALL). Unknown parameters ignored with a warning entry; unsupported standard
  ones (`patient`, `subject`, `focus`, …) and modifiers a `400`; `Prefer: handling=strict`.
  UTC dates with prefixes (no `ap`); `_count` 50 (max 500); `_total` only when asked;
  `_summary=count`; stateless keyset `cursor`; absolute fullUrls; `Cache-Control: private`
- [x] **Parameters**: Task `_id`, `identifier`, `status`, `business-status`, `priority`,
  `authored-on`, `_lastUpdated`, `_tag`; AuditEvent `_id`, `date`, `entity` (also
  `:identifier`), `subtype`, `_lastUpdated`; Provenance `_id`, `recorded`, `target` (also
  `:identifier`), `_lastUpdated`; CodeSystem `url`, `version`
- [x] **Audit resources on `/fhir`** carry absolute references to the Task (Provenance: the
  version it made); the `/api/audit` exports keep identifiers only. Reviewing an ABSTAIN
  renders the reviewer as author, not verifier
- [x] **Store**: batched event fetch per page; indexes `audit_events (decision_id, seq)` and
  `decisions (created_at, id)`; tenancy in SQL on every page and count; reads never write
- [x] **CapabilityStatement**: read, vread, search-type with every searchParam (R4 canonicals);
  `conditionalRead: not-supported`; prose updated. Auth: `read`, never `public` (a test)
- [x] **CI**: Task versions, searchsets (with next links, a warning entry, `_summary=count`) and
  the audit reads in the validator run; Postgres for every new query
- [x] **Docs**: README, API docs, the audit's status after Phase 3, this file
- Deferred: a monotonic chain clock; AuditEvent `action`/`_sort`, Provenance `agent-type`;
  `NamingSystem?value`; HEAD; `_history` Bundles; patient search; salting the input hash;
  moving `$decide`'s work item to `groupIdentifier`

## Built: FHIR core conformance, Phase 2 (2026-10-01/02)
From the [audit](../notes/fhir-and-docs-audit.md) Phase 2, revised after research (three
researchers and a critic; evidence from the HL7 validator and the official R4 definitions).
Branch `claude/fhir-phase-2`. About 6–7 days.
- [x] **Reasons never quote input** (a privacy bug found by the critic): a pack-schema
  rejection's reasons come from jsonschema messages, which quote the value, so case text
  reaches `reasons`, `Task.note` and the plaintext audit record. Build them from path and
  keyword only; a test with a marked synthetic string
- [x] **Structural R4 gate**: HL7's R4 JSON Schema (CC0), digest-pinned and patched by
  `scripts/fhir_r4_schema.py` into `standards/fhir_r4_structure.json`, checked by an iterative
  walker (`standards/fhir_structure.py`) that replaces `us_core.structure()`. Wrong JSON type,
  unknown element or resource type, bad format → `400` in every mode; a base-R4 required
  element or code → an issue under `fhir_validation`, labelled by its own profile. jsonschema
  is the test oracle. Not `fhir.resources`: it has no R4 (its R4B rejects valid R4)
- [x] **Terminology**: `standards/terminology.py` generates Mendari's CodeSystems (decision
  outcome, audit events, packs, performer queues, each pack's question labels) and
  NamingSystems (decision id, input hash, engine) from the code and packs, versioned by the
  served pack set; `GET /fhir/CodeSystem/{id}`, `/fhir/NamingSystem/{id}`; one display helper
  shared with Da Vinci output; the engine gets a NamingSystem instead of a CodeSystem URL
- [x] **`POST /fhir/$decide`** (system-level): `Parameters` (pack, version, data Bundle, focus)
  in; a `collection` Bundle out: GuidanceResponse, RequestGroup → Task, output Parameters,
  OperationOutcome for issues, and the audit trail's Provenance. Its OperationDefinition at
  `/fhir/OperationDefinition/decide`. Under BLOCK/ABSTAIN the proposed action is the fallback
- [x] **`GET /fhir/metadata`**: a CapabilityStatement built from the mounted routes (a test both
  ways), security in prose (not SMART, not OAuth), no `implementationGuide`/`instantiates`,
  `date` and ETag from the release and packs; conformance reads public
- [x] **FHIR media types** on `/fhir/…`: `application/fhir+json`; `406` for XML Accept or
  `_format`, `415` for an XML body
- [x] **Produced resources**: `meta.lastUpdated` on Task, AuditEvent, Provenance; `versionId`
  only on the immutable AuditEvent and Provenance (the Task's waits for Phase 3); no
  `meta.profile` (no profiles exist); narratives (dom-6)
- [x] **CI**: the validator loads the generated terminology (`--ig`), so a code Mendari emits
  that its CodeSystem lacks is an error; it validates the CapabilityStatement, the
  OperationDefinition, a `$decide` Bundle, Tasks and the record Bundle; an expected-warnings
  list (the AuditEvent subtype stays a Mendari code: a REST subtype would claim interactions
  that never happened)
- [x] **Docs**: API docs, README and the audit say what is served, "structural R4 check" (never
  "validated"), and that Mendari is still not a FHIR data server
- Deferred: publishing on mendari.co (launch decision); search (Phase 3); renaming performer
  systems (changes pack digests); PAS's decision identifier system; checking the
  `Communication.medium` code system

## In progress: FHIR and API docs, Phase 1, and sign-in in production (2026-10-01)
From the [FHIR and API docs audit](../notes/fhir-and-docs-audit.md); approved by the owner.
Sign-in: Google, the owner as the only admin.
- [x] No input crashes a FHIR route: a test that mistypes every element of the US Core fixtures
  finds the crash sites; a core element of the wrong JSON type (`resourceType`, `id`, `meta`,
  `subject`, any `coding`, Bundle entries) → `400`
- [x] Errors on FHIR routes (`…/fhir/decide`, `/fhir/…`) are `OperationOutcome`s,
  `application/fhir+json`; an unexpected error is a JSON `500` everywhere, never plain text
- [x] Validation errors (`422`) no longer echo the request body
- [x] An unconfigured provider → `503`; `200 BLOCK` for an invalid input documented
- [x] OpenAPI: tags, a summary and description on every operation, real synthetic examples,
  `securitySchemes` only when sign-in is on; a description that says what Mendari is
- [x] Sign-in with Google: the identity provider's token and key hosts on an explicit list
  (`MENDARI_OIDC_ENDPOINT_ORIGINS`); a role from the `email` claim only when it is verified;
  `/docs` sends a signed-out browser to sign in
- [x] Docs: `deploying-with-phi.md` (Google), the audit's Phase 1 status
- [x] Production: the owner's Google OAuth client; settings for Production only; preview's
  effective configuration equal to production's before merge; sign-in verified after deploy

## Done: the rename to Mendari (2026-09-30)
`openhealth` was taken on PyPI by another project, so OpenHealth Hub became **Mendari**
(mendari.co).
- [x] Repository renamed to `Junebugg1214/mendari` (still private)
- [x] Package, import and CLI `mendari`; settings `MENDARI_*` (the `OPENHEALTH_*` names still
  work); pack ids `mendari/…`; identifiers `https://mendari.co/…`; images `mendari-engine`
- [x] Kept: the names of the hosting resources (Vercel project, Jev connector, database);
  dated records (the overnight log and the milestone plans) carry a rename note instead of being
  rewritten
- [x] A "coming soon" page for mendari.co: `web/`, its own Vercel project `mendari-web`;
  mendari.ai and the www names redirect to it
- [ ] DNS for mendari.co pointed at Vercel (the owner's)

## Done (built; the launch is the owner's): v1.0 Public open-source launch ([plan](../notes/plans/v1.0-public-launch.md))
Approved in advance with v0.5 to v1.0 (overnight run, 2026-09-29).
- [x] Plan
- [x] Security review: threat model, security headers, dependency audit and ZAP scan in CI,
  SECURITY.md
- [x] `mendari new` and a clinician's pack-authoring guide
- [x] Docs site (MkDocs Material, `mkdocs build --strict` in CI)
- [x] Contribution flow: pack-PR workflow with the eval gate; CONTRIBUTING, CODE_OF_CONDUCT,
  GOVERNANCE, templates
- [x] Release workflow (PyPI trusted publishing on tags) and the 10-minute quickstart in CI
- [ ] Going public: public repo, PyPI release, live Hub and docs site (the owner's decision;
  logged)

## Done: v0.7 Later standards ([plan](../notes/plans/v0.7-later-standards.md))
Approved in advance with v0.5 to v1.0 (overnight run, 2026-09-29).
- [x] Plan
- [x] CQL provider (Library/$evaluate on HAPI FHIR with Clinical Reasoning); the trial pack's
  criteria in CQL, checked against `criteria` in CI (job `cql`; `mendari agreement`)
- [x] OMOP CDM inputs and population evaluation; a synthetic OMOP round trip in CI
- [x] Da Vinci CRD: `coverage-information` on the PA pack's order-sign response (validated
  against CRD 2.2.1)
- [x] Da Vinci DTR: questionnaire package for the PA pack's gaps (validated against DTR 2.2.0)
- [x] Da Vinci PAS: request Bundle and submission to a mock payer (validated against PAS 2.2.1)

## Done: v0.6 Readiness for health organizations ([plan](../notes/plans/v0.6-org-readiness.md))
Approved in advance with v0.5 to v1.0 (overnight run, 2026-09-29).
- [x] Plan
- [x] OIDC login, roles and API keys; tenancy
- [x] Deployments and governance approvals
- [x] Monitoring: observed accuracy, overrides, drift, alerts; Monitoring view
- [x] PHI mode (hash / encrypt), retention, input reads logged; deployment guide
- [x] Container image per pack; on-prem templates
- [x] Pilot check in CI (a real OpenID Connect provider, navikt/mock-oauth2-server; job `pilot-check`)

## Done: design grade phases ([`notes/design-grade.md`](../notes/design-grade.md))
Graded C+ against Apple, Airbnb and best-in-class data viz on 2026-09-29; the user approved every
phase (A, B, C) to be done and merged when CI passes.
- [x] Phase A quick wins: layout shift at 768 px 0.50 -> 0 (the pack picker's width is fixed from
  first paint); content up to 1,440 px, centred (was 1,180 px, left); view titles 28/34 px (24/30
  on phones); one-line intros; rate axes start at 0, or zoom with a break mark and "The vertical
  axis starts at 90%"; configurations named once ("Rules + Jev 1.13.0"), critical labels and
  report stats in words, drawer titles in words; 44 px targets on touch screens, 28 px sort
  buttons and summaries for a mouse; hover transitions and a 1 px press; no text under 12 px
- [x] Phase B1: `app.js` (1,847 lines) split into 15 ES modules (`main.js` boots; one per view;
  `ui.js`, `router.js`, `drawer.js`, `pack.js`, `theme.js` shared), and `table.js`, `charts.js`,
  `form.js` import what they use instead of reading globals; the server fills an import map with
  content-stamped URLs and preloads every module. Spacing on the `--space-*` scale (half steps
  2 and 6 px added), layers on `--z-*`, one toggle-group pattern (joined bar or pills, two
  sizes); a test fails on any raw spacing or z-index value in components
- [x] Phase B2: the section list runs across the top up to 1,024 px (content gets the full width;
  four stats in two rows of two, never three and one); phones get a 56 px top bar (was 104) with
  a Settings sheet for theme and pack; views load into skeletons shaped like them, marked
  aria-busy (no more collapsing to "Loading…"); evaluation runs show an indeterminate bar and the
  elapsed time; any label but the recommendation waits 5 s with a focused Undo (leaving the view
  or the page sends it)
- [x] Phase B3: `/api/stats` daily entries gain `reviewed`, `overridden`, `labelled`, `agreed`
  (read-only, counted on the decision's day; tested on both backends). Overview: a 7 / 30 days /
  All period for the stats and the outcome mix, each rate with its change against the previous
  period, sparklines for Automated and Decisions; under 7 days of history a line per day instead
  of a mostly empty chart; outcome-mix segments with room carry their name and share. Cost chart:
  configurations at the same point share one named mark, near ones are dodged with a leader, and
  the deployed, safety-first leader and cost/accuracy frontier are named; the sweep names its
  lines at their ends
- [x] Phase C: motion (the drawer slides in and out, the dialog fades and scales in, a resolved
  review card folds away, Evaluations tabs crossfade, chart highlights and the settings sheet
  ease in; none under reduced motion); controls and the Decision Card's text sections unboxed
  (boxes kept for tables, charts and review items); the favicon's mark in the top bar in the
  theme's accent; stat labels 12 px, 500, uppercase, 0.04 em; empty states with an icon, one line
  and one action
- [x] Re-grade with the same measurements (110 captures, same script): **B+** (was C+), as
  projected. No layout shift at any width, no control under 24 px on any capture, 56 px phone
  bar; an A needs identity work, clinician testing, production-scale data and a real
  screen-reader and cross-browser pass (`notes/design-grade.md`, "Re-grade")
- [x] Check the production deploy after the last merge: `ad9d3d3` live, `/`, `/api/health` and
  the modules answer 200 signed in (Postgres, 5 packs), unauthenticated requests 302 to login
- [x] Small gaps from the re-grade (`notes/design-grade.md`, "What an A still needs"), one PR each;
  log in `notes/overnight-log.md`:
  - [x] Plain words in criteria tables and Try-it answer headings
  - [x] Links that carry the pack (`?pack=` in the address)
  - [x] A transition from skeleton to content (the view fades in where its skeleton was)
  - [x] Fewer whole-subtree re-renders: sorting moves rows instead of redrawing the table

## Done: v0.5 The Hub ([plan](../notes/plans/v0.5-the-hub.md))
- [x] Plan
- [x] Shared database layer (`core/db.py`)
- [x] Signed pack archives and publisher keys
- [x] Registry service (namespaces, publisher types, keys, immutable versions, search)
- [x] CLI login / push / pull / search; `Decision.from_hub` from the registry
- [x] Shareable local-validation reports
- [x] Hub website (Next.js)
- [x] End to end: five packs published, clean-venv install and pull (CI job `hub-e2e`)
- [ ] Going public: a public registry, the site and the PyPI release (the user's decision; logged)

## Done: v0.4 Healthcare standards ([plan](../notes/plans/v0.4-healthcare-standards.md))
Approved in advance with v0.5 to v1.0 (overnight run, 2026-09-29).
- [x] Plan
- [x] US Core validation of FHIR inputs (warn by default, enforce on request; HL7 validator in CI)
- [x] Audit export as FHIR AuditEvent and Provenance
- [x] Sandbox: referral cases in FHIR (US Core; the inbox's Patients too)
- [x] CDS Hooks that fetch, under an allow-list (one outbound FHIR client for all later calls)
- [x] SMART on FHIR EHR launch (PKCE, OIDC), patient context in the dashboard
  - [x] Backend: launch, callback, server-side sessions, patient-context API
  - [x] Dashboard "This patient" view
  - [x] Sandbox launcher (smart-launcher-v2) and a scripted launch; CI job `sandbox-e2e`
- [x] Bulk FHIR `$export` into local-validation datasets (backend services auth; `mendari sandbox bulk-check` in CI)
- [x] End-to-end exit checks in CI (`sandbox-e2e`); docs

## Open items outside a milestone
- [ ] Harder inbox benchmark: paraphrased messages and held-out splits for every critical label,
  reviewed by a clinician (from v0.2; the generation tooling can be built without one)
- [ ] Trial pack: Jev-alone leaderboard rows on test and the local site (about 200 paid Jev calls;
  only when asked)
- [x] Try it on record packs: a form generated from the pack's input schema (`form.js`: fields,
  dropdowns and date pickers, editable tables for lists, JSON for anything else), with the JSON
  one click away; every behaviour-test example of the four record packs round-trips unchanged
- [ ] VoiceOver spot check of the redesigned dashboard (a person at the Mac; script in the QA report)
- [x] `$decide` references resolve: the Task's `for`/`focus` and the `subject`s are the absolute
  `fullUrl` the input gives the resource, else `Type/id` (stated as relative to the client's
  server); a `urn:uuid` input reference becomes `Type/id`. The `/api/…/fhir/decide` Task is
  unchanged: it is a standalone resource the client stores, where `Type/id` resolves

## Done: Dashboard redesign ([`notes/ui-ux-review.md`](../notes/ui-ux-review.md))
Plan approved 2026-09-29 (8 PRs, about 10 working days); the user's decisions are in the plan's
section 8 (both audiences, safety-first leaderboard, Playwright, admin-only evaluations with v0.6
roles, feedback that captures the right answer, Eastern time everywhere people look).
- [x] Phase 0: browser smoke suite (`pytest-playwright`, marker `browser`, own CI job) and
  accessibility/correctness fixes: dark-mode primary buttons and light badges reach AA, form
  fields 3:1 borders, audit records open from the keyboard, ids no longer wrap one letter per
  line, a new view starts at the top with its heading focused, no `aria-live` on `main`, every
  control named, Decide shows progress and can't be submitted twice, the sandbox notice stays on
  phones, reduced motion respected, the record example picker matches its input
- [x] Phase 0b: Eastern time everywhere people look. `MENDARI_TIMEZONE` (default
  `America/New_York`, `tzdata` added) sets calendar days (Overview daily volume), the daily call
  counters, "today" for criteria and FHIR ages, card dates and every time the dashboard shows
  (with EDT/EST, whatever the viewer's clock); `/api/health` reports it. Stored timestamps, the
  audit chain, API JSON and FHIR stay UTC
- [x] Phase 1: design tokens and theming. `style.css` rebuilt on semantic tokens (colour for both
  themes, type, space, radii, elevation, motion; no hex outside the token blocks); System / Light /
  Dark toggle, saved, applied before first paint by a head script (no flash) and following the
  system setting live; Lucide icons (ISC, vendored sprite) replace emoji; plain-word outcome and
  priority labels with the codes in tooltips; stat is a solid red chip; inline styles replaced by
  classes (only data-driven bar widths remain); `tests/test_dashboard_contrast.py`; browser tests
  for the toggle, no flash and the live system setting
- [x] Phase 2: tables and leaderboard, audit drawer. `table.js` (caption, sticky header, sortable
  keyboard headers with aria-sort, right-aligned numbers, rank, sticky first columns with edge
  shadows); leaderboard ranked safety-first with the deployed row marked, one column per critical
  label, constant columns and slots said once above the table, datasets remembered (held-out set
  first); Evaluations sub-views with URLs (`#evals/compare|report|run|history`, which also fixes
  the picker/report mismatch); per-check accuracy behind a disclosure; subgroups grouped, lowest
  first, small n marked; audit rows with compact ET times open a drawer at `#audit/<id>` (focus
  held inside, Escape returns to the row) with a hash-chain timeline and the record's JSON folded;
  feedback moved from every audit row into the drawer
- [x] Phase 3: charts. `charts.js` (no library): stacked columns, lines, scatter and horizontal
  bars as SVG drawn at the container's real width (text stays 12 px on a phone, redrawn on resize;
  colours are token classes, so a theme switch needs no redraw). Marks are one Tab stop per chart
  (arrow keys, Home, End), with tooltips on hover and focus and exact values in each aria-label;
  every chart with marks has a "View data" table. Wired in: Overview daily chart (last 30 days,
  starts at the first decision so a young deployment isn't a run of empty days; the table lists
  every day), report reliability scatter (dot area = cases, diagonal = calibrated) and threshold
  sweep (the pack's automation threshold marked, and a what-if note when the run automated
  nothing), Try-it probability bars coloured by what the label does under the pack's policy
  (block = danger, review = warning; a derived decision's criteria by their effect in its table;
  no more green block verdicts),
  and the optional ones: leaderboard cost-against-accuracy scatter (log cost, "$0" slot for free
  configurations, diamonds for configurations that missed a critical case, deployed one ringed)
  and a report confusion matrix (only mistakes shaded). Deviations from the plan text: the SVG is
  `role="group"` (not `img`) so its focusable marks stay exposed to assistive technology; the bars
  are static `role="img"` with every value in one label and printed on the chart, so no data table.
  Tests: 12 new browser tests, chart colour pairs in the contrast test, `charts.js` in the asset
  versioning. A review after the PR opened fixed a sweep label that claimed automation on the four
  packs that automate nothing, axis text read out by screen readers, a focus ring that looked like
  the deployed ring, a hover-only confusion-matrix hint and a vague cost tooltip (lessons.md)
- [x] Phase 4a: feedback backend (the right answer on a thumbs-down). `POST /api/audit/{id}/feedback`
  takes an optional `expected: {question, label}` on a thumbs-down, checked against what the
  decision answered (one of its questions, one of that question's labels, not the answer it
  gave; a derived decision's checks included). Ratings can be changed: a person's new rating
  replaces their earlier one in counts, lists and exports (`superseded_by`, set in the same
  serialised write, so "latest" never depends on timestamps), and the hash chain keeps both
  (`expected` and `replaces` in the event). Unnamed ratings of a decision count as one person, so
  a mis-click can be put right. `stats.feedback.down_with_expected`. The export fills `labels`
  where raters agree and leaves disagreements visible in `feedback`. Additive migration on both
  backends under the schema lock (three nullable columns, one more than the plan: `superseded_by`),
  which also resolves earlier duplicate ratings to each person's latest. Checked on a copy of a
  pre-4a database (chain verifies before and after). UI comes in Phase 4
- [x] Phase 4: screen redesigns. Overview: a header naming the risk level and what is deployed in
  words (codes one line below), four primary stats with what each is out of (Automated, Waiting for
  review with a link and a warning tone, Override rate, Observed accuracy with its n), one row of
  secondary facts (volume, latency, cost at a readable scale, audit chain with an icon, feedback),
  and an empty state with next steps instead of a wall of zeros. Review queue: grouped by priority
  with counts, a solid banner on stat cards with the rule's reason, the recommendation as the
  primary button, focus moving to the next card. Try it: the result leads with the outcome in
  words and colour ("Blocked · urgent", solid for BLOCK and stat), the fallback as a callout,
  criteria effects and probability bars coloured by consequence (block, review, including labels
  the policy never automates), the result focused. Feedback (decision 5): Right / Wrong pressed and
  changeable, Wrong opens an optional "What should it have been?" (the decision or one of its
  checks), audit rows show current ratings read-only, the Feedback page leads with "Wrong on
  automated" and "Ready to become tests" and links each case to its record. Decision Card: a
  document with a table of contents whose links are routes (`#card/policy`), facts as a definition
  list with a copyable digest, prose at 72ch, policy in words, and an "Automates" line that says
  whether this deployment automates at all (risk level, required providers). Evaluations: report
  stats in the same hierarchy; "Run evaluation" asks first when a remote model is deployed, naming
  the dataset, its size and the models (decision 4). Small read-only API additions:
  `GET /api/packs/{id}/datasets` (sizes), each audit row's current ratings, `stats.labelled`.
  Fixed on the way: a drawer replaced by another ran the old one's close handler and closed both
  (Phase 2), and a deep link to a section is focused on first load
- [x] Phase 5: QA pass. Keyboard walkthrough of every view on a message and a record pack (every
  Tab stop, focus indicators, unreachable or hidden focus, the main flows), an accessibility-tree
  audit of every view on three packs (headings, landmarks, names, table headers, alerts), 72
  screenshots (9 screens, 2 packs, 1440/390, light/dark) reviewed as contact sheets, contrast tests
  confirmed in required CI jobs. Fixed: a "Skip to content" button (12 Tab stops before content);
  a white focus ring on solid danger headers (blue measured 1.06:1 / 2.0:1); the phone section list
  shows edge shadows and keeps the current section in view; `[hidden]` always hides (a derived
  decision's follow-up showed every check's choices, and taps outside the selected one were
  ignored); errors announced as alerts and shown 8 s, a failed view with "Try again"; report
  panels at h2 (was h1 -> h3); header rows on the card's question tables; `aria-current` on the
  active section; no focus rectangle on headings the app focuses. 8 new browser tests (62 in all).
  The QA report with the screenshots was shared with the owner privately.
- [x] Favicon (#54): `/favicon.ico` (16/32/48 px, from `favicon.svg` via `scripts/make-favicon.py`)
  ended the only 404 left in production after the redesign
  VoiceOver itself is for a person at the Mac (the report has a two-minute script)

## Done: v0.3 Reference packs: AI Guard (the fifth and last)
Plan approved 2026-09-29 (3 PRs): runtime → the pack (risk level 2, the user's choice) →
leaderboard and deployed choice.
- [x] PR 1 runtime: `dosing` provider (dose arithmetic in code), `guard` section,
  `mendari.guard.check` and `POST /api/packs/{id}/guard` (released / held_for_review / blocked)
- [x] PR 2 `mendari/ai-guard` 0.1.0 (L2): six checks (screen: rules default, jev, llm,
  rules_and_jev, rules_and_llm; dosing in code), derived `verdict` pass / review / block,
  possible-block rule (P >= 0.3) with a nurse-triage fallback; synthetic data (calibration 300 /
  test 600, local 300) and hand-written draft sets. Rules: 11/12 blocks tuning, 4/9 held out;
  nothing released without a model (`automation_requires`) and a governance override (L2).
- [x] 0.1.1 criteria: over-the-counter advice is in scope, a dose that is too high is unsafe
  advice (labels relabelled uniformly), found by a model reader on the tuning set
- [x] PR 3 leaderboard (`evals/leaderboard.json`, 34 rows on 0.1.1): rules; Jev, Claude Sonnet 5,
  Claude Haiku 4.5 and gpt-oss-120b, alone and with the rules. With the rules every model-backed
  screen blocked 21/21 hand-written drafts to block; none released
- [x] Jev daily limit set as the user asked (calls still counted)
- [x] Deployed with the user's choice (see the deployment's `/api/health`). Live: a
  missed emergency, a stroke in words the rules don't list and an unlisted-brand overdose are
  blocked; a safe draft waits for one-step approval
- [x] 0.1.2: synthetic drafts fit their questions; leaderboard re-measured (LLM screens 0.90-0.98
  verdict accuracy, rules + Jev 0.80-0.88)
- [x] Switched to the user's new choice (see the deployment's `/api/health`). Sonnet + rules
  was the only screen that never missed a draft to block in any run

## Done: v0.3 Prior Authorization Preflight
Plan approved 2026-09-29 (3 PRs): runtime → the pack → leaderboard and deployed choice.
- [x] PR 1 runtime: derive `when` over several members (AND) and label lists in `any`/`all`;
  `criteria` answers any question label and checks may carry a field `when`
- [x] PR 2 `mendari/prior-auth-preflight` 0.1.0 (L1): service/payer requirement sets as
  derive rows, structured elements (criteria) and note elements (rules default), synthetic
  data (calibration 300 / test 600, local 300) and hand-written note sets. Rules: 7/7 red flags
  tuning, 3/6 held out; nothing automated without a model (`automation_requires`).
- [x] PR 3 leaderboard: rules; Jev with rules (all sets) and alone (hand-written sets); 3 LLMs
  alone and with rules. Every model-backed config caught 6/6 held-out red flags, none
  automated, 100 % next step held out, 0 false automation; rules_and_jev automates 33-47 %
  at $0.00003 and 0.2 s.
- [x] Deployed with the user's choice (see the deployment's `/api/health`); redeployed;
  `/api/health` confirms. Live order-sign: a complete
  lumbar MRI is submitted automatically, a knee arthroscopy with gaps goes to a person, the
  lab order is ignored, and a red flag the rules miss is caught by Jev (urgent review).

## Done: v0.3 Referral Router
Plan approved 2026-09-29 (4 PRs): runtime → FHIR `order-sign` → the pack → leaderboard and
deployed choice.
- [x] PR 1 runtime: derive rows gated with `when`, `criteria` checks `exists` and `value`,
  Task/card templates naming answers (`{specialty}`), spec 0.2 doc
- [x] PR 2 FHIR `focus_where` and CDS `order-sign` (draftOrders in context, one card per referral);
  `exists.undated`; fixture pack `referral-mini`
- [x] PR 3 `mendari/referral-router` 0.1.0 (L1): specialty (naive Bayes), urgency and clear
  question (rules), checklist (criteria), derived `next_step` with `when` rows; synthetic
  referrals (train 1500 / calibration 300 / test 600, local 300) and hand-written urgent sets.
  Rules: 16/16 tuning, 7/12 held out, and all 5 misses would have been automated, so the new
  `policy.automation_requires` allows AUTO only with a model in the urgency screen. Also:
  `mendari train` on an untrained pack, YAML errors in pack/policy reported cleanly,
  `validate` checks behaviour-test inputs against the schema.
- [x] PR 4a (#29) 0.1.1: `clear_question` escalates `yes` in rules_and_* (the rules alone sent
  clear referrals back; found in the comparison)
- [x] PR 4 leaderboard: offline, Jev (alone, with rules), 3 LLMs (alone, with rules) on
  holdout/tuning/test-100/local-100. Every model-backed config caught every urgent referral,
  none automated. rules_and_jev: 100 % next_step held out, AUTO 27-47 % with 0 false
  automation, 0.2 s, $0.00004. Jev daily limit raised with the user.
- [x] Deployed with the user's choice (see the deployment's `/api/health`); redeployed;
  `/api/health` confirms; live order-sign gives return_for_info, urgent_review and an AUTO
  schedule on three referrals, and ignores the lab order.

## Done: v0.3 Clinical Trial Prescreener
Plan approved (5 PRs): runtime prerequisites → FHIR/CDS generalised → the pack → multi-pack
dashboard and deployment → leaderboard and deployed choice.
- [x] PR 1 runtime: derive rows scoped with `in`, `criteria` provider, reasons naming the
  deciding criteria, pack-scoped provider settings
- [x] PR 2 FHIR lists/transforms and multi-resource index; CDS card wording from the pack
- [x] PR 3 `mendari/clinical-trial-prescreener` 0.1.0 (synthetic GLYCEMIA-2 study):
  7 structured criteria (`criteria`), 4 note exclusions (English rules default; Jev/LLM
  variants), derived `eligibility`, L2 policy with pregnancy/CV review flags, FHIR mapping,
  ResearchStudy, `patient-view` card; `synthetic_trial.py` (test 300, calibration 200, local
  site 150); hand-written `exclusions` (tuning) and `exclusions_holdout` (written first).
  Rules: 15/15 tuning, 8/12 holdout; local site 84% vs 99% public. Local datasets are now
  offered only to the pack whose questions they label.
- [x] PR 4 multi-pack dashboard: record inputs rendered generically (message packs unchanged),
  criteria table for a derived primary (answer, confidence, effect in the table, source,
  evidence) in Try-it, review, feedback and audit; Try-it JSON editor with the pack's behaviour
  tests (`GET /api/packs/{id}/examples`); local-site dataset preselected per pack; top-bar
  overflow at phone width fixed. Deployed with the trial pack on `notes=rules` until PR 5.
- [x] PR 5 leaderboard for the trial pack (rules, Jev, rules_and_jev, 3 LLMs alone and with
  rules; test-100, local-100, exclusions, exclusions_holdout). Live Jev `noul`: 0 errors, note
  ECE <= 0.05. Every model alone found 12/12 held-out exclusions (100% eligibility); adding
  the rules lowered eligibility to 92% held out / 87% local. Card 0.1.1 says so.
- [ ] Jev-alone rows on test and the local site (200 Jev calls; needed the next day's budget;
  now under "Open items" above)
- [x] Deployed notes reader chosen with the user (see the deployment's `/api/health`)

## Done: v0.2 Model-agnostic core
- [x] Detailed v0.2 plan approved (5 PRs: spec, Jev batching, composition, LLM, leaderboard)
- [x] Spec 0.2: `boolean` / `score` question types, derived questions (decision tables with exact
  probabilities), `min_score`/`max_score` rules; `docs/spec/decision-pack-0.2.md`; fixture pack
  `tests/fixtures/packs/trial-mini`
- [x] LLM provider via Vercel AI Gateway (Claude default; OpenAI-compatible local models),
  `mendari calibrate`, gateway-reported cost, `MENDARI_LLM_DAILY_LIMIT`
- [x] Composition: `cascade` and weighted `vote` (shared composite base; shared temperature
  calibration in `providers/calibration.py`)
- [x] Jev: one call per decision (per-decision batching, one budget unit), `noul` / `score`
  mapping (live check of `score`/`noul` deferred to the trial pack in v0.3)
- [ ] Harder inbox benchmark + held-out splits for every critical label (now under "Open items"
  above)
- [x] Leaderboard (`mendari leaderboard`, card + dashboard), inbox pack 0.2.0 variants
  (`llm`, `cascade`, `rules_and_llm`)
- [x] Leaderboard LLM rows: Claude Sonnet 5, Claude Haiku 4.5, gpt-oss-120b (each with
  `rules_and_llm` and LLM-only screens)
- [x] Jev adapter aligned with Jev's docs: answer confidence = label probability (Jev's
  `confidence` is a distribution-shape statistic), published price, 429/529 retry, real limits
- [x] Leaderboard Jev rows (jev-1.13.0, measured on production): `router=jev,
  safety_screen=rules_and_jev` on all three datasets, `router=cascade` on `test`
- [x] Deployed mix and a pinned Jev model confirmed with the user (see the deployment's
  `/api/health`)
- [x] Emergency threshold: spec 0.2 `min_probability`; inbox 0.3.0 escalates at P(urgent) ≥ 0.35
- [x] `urgency` reworded (instructions and criteria agree; doubt handled by the policy)
- [x] Leaderboard Jev rows for pack 0.3.0 (jev-1.13.0; Jev daily limit raised with the user)
- [x] Preview databases: Neon branch per preview again, deleted on PR close
  (`neon-preview-cleanup` workflow); evaluations keep the request context (OIDC) in threads
- [x] Exit: 10 configurations published incl. Claude and an open-weight model; one Jev call per
  decision in production; deployed mix catches 48/48 red-flag emergencies, none automated


---

# Done: Mendari v0.1 — Decision Pack runtime + Patient Inbox Router

## Objective
A runnable, fully synthetic reference implementation of Mendari v0.1: a Decision Pack
spec, a model-agnostic runtime, and one high-quality reference pack (Patient Inbox Router) that
routes synthetic patient messages while measuring accuracy, calibration, abstention, overrides,
latency and cost.

## Plan
- [x] Step 1: Repo scaffold (uv, pyproject, ruff, mypy, pytest, CI)
- [x] Step 2: Decision Pack spec (pydantic models + validation + risk-level rules)
- [x] Step 3: Providers: rules, naive_bayes (local ML), jev (HTTP adapter) → common `Answer` contract
- [x] Step 4: Policy engine: AUTO / HUMAN_REVIEW / ABSTAIN / BLOCK with risk caps and governance overrides
- [x] Step 5: Audit trail (SQLite, hash-chained events) + human-review queue
- [x] Step 6: Synthetic dataset generator (public benchmark + shifted "local site" dataset)
- [x] Step 7: Patient Inbox Router pack (schemas, FHIR mapping, rules, trained model, policy, tests, card)
- [x] Step 8: Evaluation harness + local validation (degradation localisation)
- [x] Step 9: FHIR R4 adapter (Communication/Patient → input, decision → Task) + CDS Hooks service
- [x] Step 10: FastAPI server + developer dashboard (overview, review, audit, evals, card, try it)
- [x] Step 11: CLI (`list validate test decide eval train card schema serve audit sandbox demo`)
- [x] Step 12: docker compose sandbox (HAPI FHIR v8.12 + engine + workflow worker)
- [x] Step 13: Docs (README, spec, architecture, ADRs) and private GitHub repo

## Notes
- Jev adapter: typed `choice` questions, ≤8 per call, 2–255 labels, probabilities must sum
  to 1, chosen label must be the argmax. Endpoint, model and key are deployment settings.
- Mendari is fully separate from other projects: its own repo, Vercel project, Jev connector,
  database and test containers. Nothing is shared.
- Core abstraction: a pack declares *questions* (labels + criteria). Providers answer questions
  with probability distributions. Policy turns answers into an outcome (ADR-0001).
- Risk caps live in the runtime; level 2–3 AUTO needs a deployer's GovernanceOverride (ADR-0002).

## Results

### Completed
- All 13 plan steps. 112 tests; ruff, ruff format and mypy clean.
- Benchmark (synthetic test, n=600): 99.3% routing accuracy, ECE 0.005, 0 false automations,
  23/23 urgent detected, 0 urgent automated.
- Local validation (local-site example, n=400): 93.2%; degradation localized to Spanish
  billing/refill phrasing; 18/18 urgent detected, 0 automated; 0 false automations.
- Docker sandbox verified: 40 seeded messages → 40 decisions → 40 FHIR Tasks
  (4 stat escalations, 10 in the review queue), exactly once.

### Deployed (2026-09-28)
- One Vercel project (a FastAPI function), Neon Postgres, Jev via a Vercel Connect connector
  (`jev-latest`, which then resolved to `jev-1.13.0`). Platform authentication on all
  deployments.
- Production smoke tests passed: live Jev routing, stat escalation, review persistence,
  audit chain, Jev eval (100% on local-site sample), CDS discovery, dashboard assets.

### Deferred (v0.2+)
- [x] Pin a concrete Jev model (v0.2).
- [x] Neon preview branching so previews don't write to the production database (v0.2).
- [x] LLM provider (v0.2). The CQL provider is roadmap v0.7.
- [ ] Remote hub (`mendari pull`, signed packs): roadmap v0.5. Server authentication: v0.6.
  AuditEvent export: v0.4.
- [x] Other four reference packs (Referral Router, PA Preflight, Trial Prescreener, AI Guard)
  (v0.3).
- [ ] Harder, clinician-reviewed synthetic benchmark: under "Open items" at the top.

### Discovered
- Keyword red-flag screens miss typos → added deterministic typo tolerance (lessons.md).
- HAPI search caching breaks naive FHIR work queues → If-Match claims (lessons.md).
- Cosmetic template edits change derived data → reproducibility CI job (lessons.md).
