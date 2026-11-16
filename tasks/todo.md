# Mendari: task tracking

**Governing plan:** the vision plan below (2026-10-06): the launch plan as corrected by
`notes/vision-audit.md`, built as prompts LP-01 to LP-23; its gates are `launch/gates.yaml`
(`uv run python scripts/check_gates.py`).

Roadmap to the full goal: [`docs/roadmap.md`](../docs/roadmap.md).

## Plan: the vision (2026-10-06), the governing plan
The target is the launch plan ("Mendari Launch Plan: Build Prompts and Owner Checklist", 3 Oct
2026) as corrected by the vision audit of `e55ed48c`: [`notes/vision-audit.md`](../notes/vision-audit.md)
(summary, gap table, corrections to the plan, the **Definition of shipped** and the build prompts
LP-01 to LP-23 in Phase 4; full evidence in `notes/vision-audit-evidence.md`). Launch-plan steps
are numbered LP-; the A and B numbers in the community-packs plan below are that plan's own.
**Owner decisions (2026-10-06):** safety first, then public (the stage-2 safety items, then the
pre-public files so the repository can go public; hidden sets and grading after; design partners
optional). **2026-11-07 (owner):** no design partners, so gate G1-2's `nda-pilot-agreement` is
waived (its attestation, "no design partners, waived", recorded 2026-11-07 at the owner's
instruction), and G2-3's `partners-3` now reads design partners as optional, waivable as the NDA
line is (its attestation the owner's). Each step is one PR; Claude merges it when CI is green
and the review found nothing medium or worse, and asks first for anything touching production,
secrets or a pack's clinical behaviour.

**Stage 2 safety** (in this order)
- [x] **LP-02 Secret scan.** Run 2026-10-06 over a mirror clone (145 refs: 6 branches, 139
  pull-request refs, no tags; 389 commits) with gitleaks 8.30.1 and trufflehog 3.98.0: gitleaks
  2 findings, both the reviewed `deploy/onprem/.env.example` false positive; trufflehog 0. The
  private report is in the owner's folder (locations only); nothing to rotate. Then (#152):
  one scan, `scripts/secret_scan.sh` (gitleaks, pinned there only), over every commit of every
  ref with merge resolutions included (`--diff-merges=remerge`: without it git shows no diff
  for a merge, so what a merge alone added was never read, by any scan, until now); a git error,
  which gitleaks logs and then calls clean, fails it, as does a `.gitleaksignore`. It runs on
  every pull request, weekly and by hand over every ref (`secrets.yml`: each branch, tag and
  pull request's head, fetched with the read-only token, never stored), and in the release
  build over the history it is built from (LP-02's "tag run": LP-13 starts nothing on a tag
  push). The positive control (`scripts/secret_scan_control.sh`) plants two keys made up at run
  time where only a full scan looks, one only on a pull request's head and one only in a merge's
  resolution, and both must be found, in each of those runs and in
  `tests/test_secret_scan.py`. A full scan of this repository's refs, merges included: 283
  commits, clean. Gate G1-2's `secret-scan` is now met; its `rotation` attestation, the
  owner's, was given in chat on 2026-11-07: pointed to the private report's Outcome section, the
  owner picked "None found: attest", recorded at their instruction (`launch/gates.yaml`, by the
  owner in chat, commit `b7b3d74c`).
  **Owner, once public:** GitHub turns a
  schedule off after 60 days with no activity; check monthly that `secrets.yml` ran.
- [x] **LP-01 Inbox gate** merged in #130 (gate, rationale, three tests). The invariant for
  every maintained pack: `tests/test_safety_invariant.py` runs every configuration that needs no
  service on every declared dataset. By default no true critical case is automated (1,366
  checked); under a governance approval (risk 2-3) an automated one carries the correct primary
  recommendation (the trial prescreener automates 104 of 159, all a correct `not_eligible`).
  **Owner decision (2026-10-06):** the spec's "must never be automated" narrowed to exactly
  that (spec §8, `CriticalLabel`); no pack changed. Not built, on purpose: a separate
  `reference-pack` CI step (the test job already runs the test on every PR, and that job proves
  the rebuilt data and models byte-identical to what it tests), and a load rule for critical
  slots (it would misfire on exact providers such as `dosing` and `criteria`; the measured
  invariant is the guard).
- [x] **LP-07** Shared evaluation reports leave no case behind. `mendari.site-report/1`
  (`evals/share.py`): one closed pydantic model (extra keys refused, strict types, every string
  a short name, every number bounded) built by the client and checked by the registry before
  anything is stored. Dropped: `missed_ids` (sent before for every missed critical case), the
  dataset's name and hash, the override's approver and reference (now `governance_override:
  true`), case ids, and subgroups (their values come from a site's data, so a clinician's name
  or a date would pass any pattern; they return when packs declare each subgroup's values);
  numbers rounded and calibration bins under 10 refused on both sides. The review's fixes: the
  registry checks the token before reading at most 2 MB, never echoes what it refuses (NaN in a
  body was a 500), and leaves out stored rows that do not validate. The focused check's three
  lows: the body is parsed, then validated (JSON-mode validation of a long list used ~900 MB for
  1.8 MB); error locations show only field names and positions; question, label, slot and
  variant names must be the published pack's (`describe` records them at publish; a version
  published before is refused with 409). `mendari eval --share-out
  FILE` writes the payload to read first. The registry lists reports as `verified: false` and
  the Hub no longer shows them: any publisher token can still submit under any site name.
  Left for LP-22 (with site signatures): counts in, rates out (the registry computing every
  rate), a keyed dataset commitment, one live submission per site.
  - [x] Follow-up (found in the LP-10c review; #160, #161): a composite slot's evaluation can
    be shared. Its model goes as recorded (`member:model`, joined by ` + `, or ` → ` for a
    cascade) and a failed member's error as `member:code`. The registry records each composite's
    members at publish (`names.members`) and refuses other members, another order, or failed
    members that are not one composite's that ran; a version published before answers 409.
    **The closed error code set:** a provider error's code is one of `ProviderErrorCode`
    (`errors.py`, a `Literal`, so mypy refuses any other): `budget_exhausted`,
    `credential_unavailable`, `insufficient_credit`, `invalid_endpoint`, `invalid_input`,
    `invalid_response`, `not_configured`, `provider_unavailable`, `quota_exhausted`,
    `rate_limited`, `time_budget`, `timeout`, `unavailable`. With `derived_input_unavailable` (a
    derived question whose inputs failed) they make `FAILURE_CODES`, every code a decision can
    record, and the only codes a shared report's errors may name, plain or after `member:` (they
    were checked as names, so `Jane_Doe.chest-pain` was stored). Spec §5 lists them; a test keeps
    that list equal to `FAILURE_CODES`. A new code goes in the `Literal` and §5 together, and a
    registry refuses reports carrying it until it runs the release that knows it.
- [x] **LP-04** The synthetic-example label and display names, as optional 0.2 fields (no
  published engine yet, so no 0.3): `card.provenance` (`synthetic`, `de_identified`,
  `site_records`) and question `display` and `names`. The line every surface shows is derived
  (`core/status.py`), never written by a pack: "Synthetic example. Not clinically reviewed. Not
  for patient care." with `synthetic`, else "Not clinically reviewed." (no way exists yet to
  record a clinician's review of a digest, so no pack can claim one). Shown on the Decision
  Card's first line, `/api/packs`, the SMART session's packs, the dashboard (overview, card, Try
  it, review queue, audit drawer, patient view), CDS Hooks discovery and every card's detail,
  the registry's metadata and the Hub, and the README. The five packs set `provenance:
  synthetic` and display names (versions bumped, rebuilt: data and models byte-identical;
  9,958 decisions identical to main's in every offline configuration). Rendering display names
  instead of codes is LP-05; note there that a pack's own names are its author's claim (a
  community pack could name `block` "Safe"), so the code stays one click away.
- [x] **LP-05** One versioned notice on every surface, a recorded acknowledgement before Try it,
  a plain-language result view. Split in two.
  - [x] **LP-05a** `mendari/notices.py`: "Research preview. Not a medical device. Not for
    emergencies: patients should call 911.", version 1. Stamped by the server into every page
    (`<!-- mendari:notice -->`: the dashboard shell, launch view included, and `/docs`), the
    OpenAPI description, every Decision Card (second line), CDS Hooks discovery and every
    card, every JSON decide answer (`notice`, the SMART launch's too; not the stored record and
    not a FHIR `$decide` resource), `/docs/reference`, the launch and sign-in error pages, the
    drawer (which covers the page), and the Hub layout (a test holds the words equal). A
    signed-in person who may decide or review acknowledges once per version when the dashboard
    opens (a modal Escape does not skip): `GET /api/notice`, `POST /api/notice/acknowledge`
    (chained once as `notice.acknowledged` with the actor; security log lines for both and for
    refusals). Until then deciding, reviewing, feedback and minting a key or client answer `403
    NoticeNotAcknowledged` (header `Mendari-Notice-Required`, no sign-in challenge). Keys,
    clients, EHR launches and open mode are never asked; an acknowledgement is a person's,
    across organizations; `/api/health` shows the mode. The review found no high or medium
    finding; its 11 lows are fixed, and the focused check's 4 lows (the dialog says its own
    errors and offers Sign out; a refusal mid-session asks again and retries once; docs on the
    header). Checked locally (Actions blocked by billing): 2,742 passed with Postgres, 159
    browser tests passed. `MENDARI_NOTICE_ACKNOWLEDGEMENT`
    (`required`, or `off`; off in the test suite as rate limits are). Deferred: a deployment
    data policy (`synthetic`/`phi`) for the badge and the acknowledgement's wording at a PHI
    pilot.
  - [x] **LP-05b** Plain-language result view and axe-core checks (#143, merged 2026-10-07). Try it's result shows
    the pack's names (`display`, `names`; else the key as words, as the server spells it), the
    reasons a person reads (a rule's own sentence, a hold's reason, the engine's reasons said
    plainly) and each answer by name; the codes sit beside the names, hidden until "Show codes" (a
    pack's names are its author's claim). Probabilities, providers, thresholds, timings and the
    reasons as the engine gave them moved to the decision record's drawer ("Engine detail", its
    charts now drawn there). The review queue's cards and buttons use the names too (the codes stay
    what is sent). axe-core 4.14.0 (MPL-2.0) vendored for tests only, pinned and checked like
    Swagger UI (`scripts/vendor_docs_ui.py` now handles both; Dependabot watches it): every view of
    every reference pack, an open drawer, the result, `/docs` and `/docs/reference` (Swagger UI's
    own markup left out), in both themes, WCAG 2.0 to 2.2 A and AA, after entrance animations
    finish. It found one real issue, fixed: the audit list's record buttons were named without the
    time they show (WCAG 2.5.3). Deferred still: the deployment data policy (`synthetic`/`phi`) for
    the badge, at a PHI pilot.
- [x] **LP-06** Automation hold (the kill switch, brought forward from A13). A hold is a row
  (`automation_holds`): an organization (or every one), a pack (or every pack), a digest (or
  every version); set and lifted with a reason, chained (`automation.held`, `.lifted`). Each
  decision looks up the holds covering it (`Decision.hold_source`, reused at most 5 s on an
  instance) and policy turns `AUTO` into `HUMAN_REVIEW` with the same recommendation and
  priority (`apply_policy(hold=...)`, `_cap`), nothing else. `GET/POST /api/holds`, `DELETE
  /api/holds/{id}` (`approve`; an organization's own), the Decision Card's controls and the
  overview's banner, `/api/health` `holds`, and a break-glass that writes the database with the
  server down: `mendari automation hold|lift|list` (`--all-orgs` only there). Over an
  organization's daily decisions quota, local providers now still answer (remote calls fail
  with the quota, through a context variable on the call budget) and the quota holds the
  decision: an emergency still reaches a nurse at `stat` (before, every provider failed).
  `docs/operations/automation-hold.md`. The review (1 medium, 7 lows) fixed: the break-glass
  takes `--db` or `$DATABASE_URL`, says where it wrote, and refuses a missing database, one that
  never served a decision, an unknown pack and a malformed digest (it used to create a fresh
  database and report success); a hold set here is not undone by a look-up already reading (a
  generation counter); `mendari decide --db` honours holds; over quota, Jev and LLM refuse before
  fetching a credential; a held decision keeps its other reasons; the API refuses a digest it
  does not serve; the command-line actor is sanitized; the docs no longer show a key setting a
  hold. And an older gap: PAS submitted any `submit_ready` decision, held or unreviewed, to the
  payer; it now sends only one that was automated or a reviewer resolved as `submit_ready`.
  The focused check's 6 lows: PAS reads the label the review chained (an outcome reported
  later cannot stand in); the break-glass reads a database before it opens one (nothing is
  created in another application's), accepts any database holding decisions, deployments or
  holds (`list` and `lift` need only Mendari's tables), shows a URL without its password in any
  form, and refuses a digest that database never served; README and the PAS docstring say a
  held decision needs a review.
- [x] **LP-08** Hosted data protection (#141, merged and deployed 2026-10-06 with LP-04, LP-05a and
  LP-06, #138 to #140). The deployment's keys are the owner's (set before the merge). A malformed
  `CRON_SECRET` now turns the purge off with a warning instead of stopping the server. The hosted
  site runs `encrypt` unless told `hash` and refuses `off` (the root `app.py` marks it hosted even
  when Vercel hides its system variables); elsewhere sign-in or the EHR launch needs a chosen mode
  (`off` written out: a synthetic sandbox, with a warning); and a database once opened under `hash`
  or `encrypt` is marked and never takes plaintext again (a server under `off` refuses to start on
  it, a decision or note refuses before any provider runs). What a record holds itself (an input
  kept under `off`; evidence and resources' logical ids on records from before 2026-10-02) is shown
  only to reviewers and admins, each read chained and logged (`input_read`); the audit list never
  carries it. One decision's kept input, evidence and notes are erased on request (`mendari phi
  erase`, admin `POST /api/audit/{id}/erase`; `still_in_chain` says what only retiring the database
  removes). Retention: `MENDARI_RETENTION_DAYS` (30, 1 to 36500), batches of 500, a chained summary
  (`retention.purged`) and `last_purge` in health; `mendari phi purge --days 0 --yes`; a Vercel Cron
  route with `CRON_SECRET`. A test asks every read route as every principal over old and new records
  (negative controls fail it). Reviews: the adversarial review (2 medium, 5 low), a focused check (5
  low), a check of the spec additions (1 medium: a command run off Vercel fell back to `off` on the
  hosted database, fixed by the protected mark; 3 low) and a check of that fix (3 low), all fixed.
  **Owner decision (2026-10-07): refuse.** Outside Vercel too, sign-in on with `MENDARI_PHI_MODE`
  `off` (or unset) stops at startup, on SQLite and Postgres alike; the compose sign-in sandbox and
  CI's `sandbox-e2e` move to `encrypt` with generated keys (locally, once, in the gitignored `.env`).
  An EHR launch with sign-in off keeps its warning (a separate decision). The build PR replaces gate
  a5's pending probe with its tests.
  **Built** (#173, branch
  `claude/lp08-refuse-off-with-signin`): outside Vercel too, sign-in on (OIDC issuer and client id) with `MENDARI_PHI_MODE` `off`,
  written out or unset, refuses to start in one line, on SQLite and Postgres alike, before the
  database is opened (`require_protection`). An EHR launch with sign-in off keeps its
  chosen-mode check and warning (not refused yet: a separate decision). The compose sandbox's
  `engine-signin` runs `encrypt`, its two keys read from the shell or the gitignored `.env`
  (`${VAR:-}`, never committed; generated once, README; to start over, remove that engine and
  its volume alone), checked by
  hand on Docker (keyless it stops naming the key it lacks; with keys `smart check` and both
  `--expect-org pilot` launches pass). CI's `sandbox-e2e` writes masked throwaway keys to
  `$GITHUB_ENV`; `check_doc_snippets.py` runs its signed-in pass under `encrypt`. Every signed-in
  test runs `encrypt_mode` (the refusal alone failed 239 SQLite cases): 22 test files, 17
  fixtures and helpers and 11 tests gained the call, 6 tests changed their assertions or setup
  (a reviewer's read now chains `input.read` after an outcome; a decision recorded beside a
  signed-in server goes through `encrypted_store`; health says `encrypt`), 1 test was replaced by
  2 refusal tests. Gate a5's pending probe is replaced by
  `test_sign_in_refuses_plaintext_whatever_the_database` (SQLite and Postgres),
  `test_the_cli_refuses_sign_in_without_protection` and
  `test_the_signed_in_templates_choose_a_mode`: a5 now counts as met. Review (2 low, both
  fixed): the README's `.env` step now ends a last line left without its newline and adds each
  key only if missing (it had glued the first key onto that line and, on rerun, added a second
  hash key, a new epoch); the reset no longer says `down -v`, which also removed
  `mendari-data` (the open engine's database and the registry's packs). Rebased on #177, whose
  invitation tests sign in: their helpers in `test_invite_page.py`, `test_notice.py` and
  `browser/test_invitations.py` (since moved to `browser/access_site.py`) gained `encrypt_mode`
  too. Rebased again on main after LP-12g, 12h, 12i, LP-16 and #180: the signed-in tests they
  added gained `encrypt_mode` (`test_administrative_audit.py`'s server, `test_key_presets.py`'s,
  `test_chain_orgs.py`'s memberships-mark test, `browser/test_credentials.py`'s claims-mode
  site), no assertion changed, and `check_doc_snippets.py`'s key-preset pass (LP-12i) signs in
  under `encrypt` as its signed-in pass does.
- [x] **LP-09** Key rotation (#142, merged 2026-10-07). The PHI key: a ring, `MENDARI_PHI_KEY` seals and
  `MENDARI_PHI_READ_KEYS` (up to 4, checked at startup, never shown) only opens; from the first
  rotation on each token names its key (`v2:<key id>:`; before it, the old format, so a release
  before LP-09 still reads everything). `mendari phi rotate --to <phi_keys.current>` re-seals
  inputs, their details and notes in batches, outside the chain lock, with compare-and-set writes
  and one chained `phi.rotated` per batch; an interrupted run is run again; a value no key opens is
  listed, never lost. A key a rotation moved values off is retired (a stale deployment on it refuses
  to seal); a retired key is never a target again. `--check` (marking nothing) opens everything with
  the current key alone, recomputes input hashes and note commitments, and lists read keys not
  retired. Every key a deployment held is marked known, so `verify --notes` tells a missing key from
  an altered note. The input hash key: one chained `input_hash.epoch` per key id; hashes never
  re-keyed. API keys: `POST /api/auth/keys/{id}/rotate` (same name, role and re-checked scope; the
  old key works for an overlap, 24 h unless asked, then expires; 404/409/422; a lost answer is
  recovered by revoking the replacement); every `keys` POST waits for the notice;
  `admin.key.rotated`, `auth.key.replaced_used`. The runbook in docs/deploying-with-phi.md (reader
  first, swap, re-seal, check, drop). Reviews: a design review (3 lenses, 17 changes), an
  adversarial review (1 high: a second rotation by the documented steps was refused and its advice
  retired the deployed key; 1 medium; 5 low) and a focused check (4 low), all fixed; refuted
  suggestions kept out on purpose (retiring every read key at the start would retire the new key if
  run one step early).
- [x] **LP-13** Pre-public files and release gates (#145, merged 2026-10-07). `NOTICE` generated from
  `licenses/notice.toml` by `scripts/build_notice.py` (`--check`): the project's copyright line,
  Swagger UI (its own NOTICE carried), Lucide (its licence file now carries Feather's MIT text),
  HL7 FHIR R4 and US Core, LOINC, SNOMED CT, CPT, ICD-10-CM and X12 notices, and axe-core as
  test-only; it ships in the sdist, the wheel (`.dist-info/licenses`) and the image.
  `scripts/check_excluded.py` refuses `CLAUDE.md`, `AGENTS.md`, `.claude/`, `tasks/`, `notes/` and
  `.github/` at any depth in the sdist, the wheel, the docs site and the image (CI `hub-e2e`,
  `docs`, `docker`; the release build). DCO: CONTRIBUTING says inbound = outbound Apache-2.0, the
  DCO 1.1 sign-off, how to fix a missing one and who merges; `scripts/check_dco.py` checks every
  non-merge commit a PR brings (author's address in a `Signed-off-by:`; Dependabot and GitHub
  Actions exempt; history up to main's `bb341182` exempt), run by CI's `dco` job. SECURITY.md: a 90-day
  coordinated-disclosure window; `web/.well-known/security.txt` (the reporting form, no address;
  expires 2027-10-01, a test fails 30 days before). Releases: `images.yml` removed; `release.yml`
  starts only by hand (`workflow_dispatch` run from the tag itself, with `publish` off by default;
  run from a branch it refuses), every job checks out the run's one commit, it checks the tag
  names that commit and the version is the same in pyproject, `__init__`, uv.lock and the compose
  tags with its `CHANGELOG.md` section newest, runs the tests, builds and checks the packages,
  builds, tests and checks the six images and saves them; the publishing jobs need that build, a
  public repository (job `if`) and their environment's approval, then check through the API, as
  their first step, that the repository is public and the environment has required reviewers
  (failing closed: an environment a job names is created unprotected if missing), and push the
  saved images as tested. LOINC's notice is its licence's text word for word. Review: one
  adversarial review, each finding checked by a skeptic: 1 medium (run from `main` as the steps
  said, the `v*` rule on the environments refused every publish; the run is now bound to the tag)
  and 4 low (each job resolved the tag anew; the guard's order as documented; LOINC's notice
  missing its years; the DCO start point before #143), all fixed.
  `SPDX-License-Identifier: Apache-2.0` on every source file under src/, tests/, scripts/ and hub/
  (not vendored files or the shown snippets), `tests/test_spdx.py`. Version kept in pyproject and
  `__init__` with a test across all four places (a dynamic version would change the Vercel build).
  **Owner decisions, confirmed (the recommended defaults; owner, 2026-11-07, in chat: "the patent
  gate is clear and I approve everything on my part"):** the copyright holder is "The Mendari
  authors" (NOTICE); the existing history is published as it is (untouched), provisional on
  counsel (LP-14's counsel item); `tasks/` and `notes/` go public with the repository and stay out
  of every package, the site and the image, enforced. Deploy `web/` again (owner) once the
  repository is public, so security.txt's contact works.

- [x] **LP-14** A redistributable reference pack and a licence inventory (#146, merged 2026-10-07). The
  prior-authorization pack's 15 CPT codes are replaced one for one by fictional codes (`FX101` to
  `FX903`, `https://mendari.co/fhir/CodeSystem/fictional-procedures`, named "not CPT, never for
  billing"; a sandbox CodeSystem for the validator) in its logic, mapping, tests, generator, the
  hand-written documentation-note sets and every test; the pack is 0.2.0 and decides all 1,272
  examples, datasets and tests exactly as before with the codes swapped (compared case by case).
  PAS sends only a code its value set takes (below). Manifest: `license` must be an SPDX expression;
  `code_systems` (url, name,
  `licence_note`) lists the systems a pack uses; a licensed one (CPT, SNOMED CT, MedDRA, X12,
  CDT, by url or OID) needs its `licence_note`, and a pack any of whose files names one without it does not
  load (every spec version; the Decision Card lists them). The trial prescreener and the referral
  router (0.1.4) declare SNOMED CT with its terms. `scripts/check_licences.py` and
  `tests/test_licence_inventory.py`: the Python runtime closure (37 packages, walked with
  importlib.metadata, each checked against uv.lock) and the Hub's 142 production packages, against
  a permissive allow-list; named exceptions with reasons (certifi MPL-2.0, psycopg LGPL-3.0, sharp's
  libvips LGPL, caniuse-lite CC-BY-4.0). `tests/test_code_systems.py`: no CPT in any tracked pack,
  dataset or fixture; a licensed system only where declared or in four US Core fixtures allowed
  with why. NOTICE drops CPT.
  **Owner decision (2026-10-07): swap.** The SNOMED CT codes in everything that ships (the trial
  prescreener and the referral router, `datasets/trial-local-site-example.jsonl`, the generators
  and converters) are replaced by fictional codes, as CPT was, in one build PR: both packs bumped
  and rebuilt, the wheel and sdist scanned for SNOMED CT, an RxNorm line in NOTICE. Accepted with
  it: until LP-23's site code map, the referral router matches no real SNOMED-coded referral and the
  trial pack reads real diagnoses through ICD-10-CM only. Counsel is asked in parallel (the owner
  sends the memo) about what stays: the repository's tests, fixtures, docs and history; ICD-10-CM;
  the images (the Debian base and psycopg-binary); the X12 values in `standards/pas.py`.
  - [x] **SNOMED CT swapped out of every package** (owner's choice 2026-10-07, #174, open): the
    six SNOMED CT codes in the wheel, the sdist and the images are replaced one for one by
    fictional codes, as CPT was: the trial prescreener's conditions (`FC101` to `FC203`,
    `https://mendari.co/fhir/CodeSystem/fictional-conditions`) in its criteria, CQL, criterion
    text, the generator and `datasets/trial-local-site-example.jsonl` (its ResearchStudy is
    ICD-10-CM E11; the example's strokes, written as cerebral infarction, carry `FC203`, so each
    coding's display is the published CodeSystem's), and the referral router's FHIR focus
    (`FR101`, `.../fictional-referrals`, in the sandbox's referrals and the US Core referral
    fixtures' category; their SNOMED CT `code` stays for US Core's binding); each system named "not
    SNOMED CT", a sandbox CodeSystem for the validator; the converters (record to FHIR, OMOP)
    label the fictional codes with their own system. Trial 0.2.0, referral router 0.2.0, both
    rebuilt: every decision identical with the codes swapped (3,796 decisions and 150 record
    Bundles, case by case). NOTICE moves SNOMED CT to "the source repository also carries" and
    adds NLM's RxNorm acknowledgement. `tests/test_code_systems.py` and `scripts/check_snomed.py`
    (CI `hub-e2e` and the release build, on the built wheel and sdist): no SNOMED CT concept id
    in src/, packs/ or datasets/ (the nine the repository has used, and any other number
    written as one: a concept's partition and SNOMED CT's Verhoeff check digit; in the vendored
    swagger-ui only the nine), no concept URI, and the url or OID only in the three modules that
    must name the system; the test also scans the sdist's other files and what the images'
    `/licenses` is written from since #180 (NOTICE, licenses/image). **Accepted with it:** until a site code map exists (LP-23), the
    referral router matches no real SNOMED-coded referral through FHIR and the trial pack reads
    real diagnoses only through ICD-10-CM; both cards say so. **Wider than the owner's wording
    (review, 2026-10-08; the owner confirmed it the same day, in chat: merge as is, LP-23's
    system-aware code selector fixes it):** through FHIR the trial pack reads only a
    Condition's first coding (`code.coding[0]`, unchanged), so a Condition coded first in SNOMED
    CT and second in ICD-10-CM, which 0.1.5 found through its SNOMED CT code, is not found
    either: type 2 diabetes `unknown`, a recent heart attack or stroke missed (still
    HUMAN_REVIEW at risk level 2). The card, both changelogs and README say so, and
    `tests/test_trial_pack.py` pins the ordering for LP-23.
  **For counsel (not decided here; the owner sends the memo):** whether SNOMED CT codes may stay
  in the repository's tests and fixtures (the US Core fixtures code procedures and referrals in
  it), the scan's own list (`scripts/check_snomed.py`), the notes and the history (earlier
  commits carry the old packs and dataset); SNOMED CT needs an Affiliate Licence, free in member
  territories, low-income countries and qualifying research, paid elsewhere. What ICD-10-CM's
  terms need (free to use from NCHS, adapted from WHO's copyrighted ICD-10 for U.S. government
  purposes). What the published container images must carry: the Debian base image's GPL and
  LGPL packages, and the LGPL-3.0 PostgreSQL driver: psycopg-binary bundles libpq, OpenSSL 3 and
  1.1.1, libxcrypt and keyutils (LGPL-2.1+), Cyrus SASL and MIT Kerberos (attribution clauses),
  OpenLDAP, PCRE2 and libselinux; since #180 every image carries their licence texts and notices
  in `/licenses`, with its source pushed beside it, the approach counsel is asked to confirm
  ("Image licence compliance" below). Whether the X12 code values in
  `src/mendari/standards/pas.py` (shipped) may stay under X12's terms.
  **For the owner (conflict with the build prompt):** LP-14 asks to move a copyleft driver to an optional extra; psycopg is a hard
  dependency because the hosted deployment installs from the lock without extras, so moving it
  would change how production installs. Kept as is, with a named exception: confirmed by the
  owner on 2026-11-07 (in chat: "the patent gate is clear and I approve everything on my part").
  An extra would need the Vercel install to name it, checked on a preview first.
  PAS: the fictional codes are in no payer's system, and Da Vinci PAS binds the requested
  service (required) to X12 278 Requested Service Type, so PAS sends only CPT, HCPCS or
  ICD-10-PCS, and the synthetic orders the samples send also carry a public-domain ICD-10-PCS
  code (checked with the pinned HL7 validator: no errors). ICD-10-PCS is HIPAA's inpatient code
  set: on the sandbox's outpatient, professional request it stands in for the CPT code a real
  order carries; a real site sends CPT or HCPCS. A focused check of the fixes found 2 more
  medium (an escaped MedDRA url skipped at load; THO's X12, CDT and MedDRA spellings missed) and
  9 low (community packs refused by the test; CPT in the manifest's words unchecked; one-word
  licence fields; more escaped spellings; the PAS wording; the psycopg, ICD-10-CM, SNOMED CT and
  ICD-10-PCS wording and records), all fixed. Review: one adversarial review, each finding checked by
  a skeptic: 2 medium (the PAS sample and the US Core fixture failed the HL7 validator with
  fictional codes alone; the licence reader let a stated copyleft licence through behind a
  permissive classifier) and 12 low (the scan's spellings: JSON-escaped slashes, bare OIDs,
  SNOMED concept URIs, MedDRA's own url, X12 and CDT; a test that could not see bare CPT codes
  coming back; a licence of nested parentheses crashing the loader; `WITH OR` accepted; any
  `urn:` system refused; the SNOMED CT, ICD-10-CM and psycopg wording; the trial dataset's
  SNOMED CT codes left out of the record), all fixed.
  **X12 (owner, 2026-10-07): code values out of the packages; a site supplies them as settings.**
  After a licensing analysis the owner received in October 2026 (not a legal opinion). Built on
  `claude/lp14-x12-settings` (#179, draft): nothing in src/, packs/, datasets/, README or NOTICE
  carries an X12 code value; the code-system urls (names, from HL7's CC0 PAS guide) stay.
  Inventory of what shipped: the request type and certification type `request_bundle` filled in,
  the Coverage's code for self and the payer's and requester's entity codes `pas.py` filled in,
  the sandbox payer's review action, the service type in `mendari pas build --help`, and two
  docstrings naming review actions (none in packs, datasets, the dashboard, the snippets or the
  OpenAPI examples). Now: `MENDARI_PAS_REQUEST_TYPE`, `MENDARI_PAS_CERTIFICATION_TYPE` and
  `MENDARI_PAS_SERVICE_TYPE` (the request, then the order, then the setting; flags
  `--request-type`, `--certification-type`; API fields `request_type`, `certification_type`),
  required by PAS, so one nothing gives is a `PasError` naming its setting; the optional
  `MENDARI_PAS_SELF_RELATIONSHIP`, `MENDARI_PAS_PAYER_ENTITY`, `MENDARI_PAS_REQUESTER_ENTITY`,
  left out when unset (PAS 2.2.1: the X12 relationship slice is 0..1 and Organization.type
  0..*; its `self-beneficiary` rule then needs the subscriber, so the Coverage names the patient,
  checked with the pinned HL7 validator, which fails the request without it). The sandbox payer's
  review action is `MENDARI_SANDBOX_PAYER_REVIEW_ACTION`; unset, it omits the code (optional in
  PAS's `extension-reviewAction`; validated), and its disposition no longer quotes X12's words.
  With the old values as settings the sample request is byte for byte what it was (a digest
  test); the prior-auth pack is unchanged (no pack file touched). `tests/test_x12_values.py`
  scans every shipped file (data as data, Python as code, every line that speaks of X12).
  **Conflict with the build prompt (for the owner):** it asked to list the X12 values left in
  repository-only files in NOTICE's repository section, but NOTICE ships in the sdist, the wheel
  and the image, so NOTICE names the files and not the values (confirmed, owner, 2026-11-07).
  **Open, for X12's written
  answer:** the values the repository still carries as test and sandbox configuration
  (`tests/test_pas.py`, `tests/test_notes.py`, `tests/test_x12_values.py`,
  `tests/test_key_presets.py`, `scripts/davinci_sample.py`, `docker-compose.yml`), listed here,
  and quoted in `notes/vision-audit-evidence.md`: request type HS (1525), certification type I
  (1322), service type 62 (1365), relationship 18 (1069), entity codes PR and 1P (98), review
  actions A1 and A3 (306), all without their descriptions, plus A4 in the scan's list of former
  values. NOTICE's repository section names each of these files, this one included.
  Adversarial review (3 lows), all fixed: the PHI guide said the compose sandbox sets every PAS
  setting, but it sets no service type (its synthetic orders carry none and are not one kind of
  service, so each request gives it; the guide and README now say so, and no value was added);
  NOTICE's inventory left out this file; and the compose payer's `:-A1` turned an empty variable
  into A1, so its no-code answer could not be tried (now `-A1`: only an unset variable defaults).
  Rebased on main after LP-12i-i (#176): its preset test built a PAS request relying on the
  request and certification types Mendari used to fill in; it now gives them with the request,
  and NOTICE lists the file (it also gives a service type).

- [x] **LP-15** What the docs promise, and the local loop. Split in three PRs (recommended):
  - [x] **LP-15a** The local loop (#147, merged 2026-10-07). One top-level error handler for every `mendari`
    command: one line per problem (`error: ...`, naming the file, flag, URL origin or database
    without its password, and the fix), exit 1, never a traceback and never a library's own
    message (it can carry case text: an unknown error is its type only); `--debug` on any
    command or `MENDARI_DEBUG=1` lets it through; usage errors one line and exit 1 (argparse's
    was 2; `eval --fail-on-degradation` keeps 2); Ctrl-C `interrupted`, 130; `--version`; ports
    checked before a server starts. Behaviour tests strict: `expect` required, non-empty, only
    the six keys, each value one the pack can decide (outcome, priority, the primary's label,
    the pack's questions and labels), no case key beyond `name`, `input`, `expect`; a case named
    by number and name; `mendari test` on a pack with no tests fails. `validate` reads every
    declared dataset (JSON per line, inputs as a decision checks them, labels the pack's own; ten
    problems a dataset at most, never case text); `eval` refuses such a dataset instead of
    crashing (KeyError, AttributeError before). A typed provider setting (`--provider`,
    `leaderboard --run`, `omop run`, `serve --provider`) that applies to nothing in the pack is
    an error; `MENDARI_PROVIDERS` only warns in the CLI and is reported by a server
    (`provider_settings_unused` in `/api/health`'s detail and the startup log), never a failed
    start. `audit verify`, `audit export`, `feedback export` and `monitor` open the database
    read-only (SQLite `mode=ro` and `query_only`; Postgres `READ ONLY` transactions): nothing
    created, migrated, marked or rewritten (journal mode kept), a missing or foreign database is
    one line; an older, unmigrated database is verified as it is, and the commands that need
    its newer columns say to start the server on it once. `phi purge`, `phi erase` and
    `notes erase` refuse a missing database too; the automation and rotation commands raise like
    every other. Spec §10 (every spec version, as LP-04 and LP-14 did before a release), the
    authoring guide, the README, the threat model rows T1 and D7, the CHANGELOG.
    Review: one adversarial review (three lenses), each finding checked by a skeptic: 16 low,
    all fixed (a file's content given as its path printed whole, now never; Ctrl-C let a
    parallel eval run its whole queue, now cancelled; `MENDARI_DEBUG=off` meant on; a broken
    input in the test rewrote the repository's pack data; two inputs were usage errors that
    never reached their command; a pool's log line beside the error; uvicorn's exit 3; JSON
    files and an answered HTTP status not named; a closed pipe; dataset labels echoed (a site's
    can hold case text); `serve --provider` with a mistyped variant started anyway; a wrong
    community-pack hint; sidecar files left beside a WAL database (read as a file now when no
    server has it open); a `postgresql+psycopg://` URL printed with its password (refused);
    purge and erase refusing a database from before LP-06; `audit export` refusing an older
    database it can read), 3 refuted.
    **Owner decisions, confirmed (the defaults; owner, 2026-11-07, in chat: "the patent gate is
    clear and I approve everything on my part"):** `monitor` keeps exit 1 for alerts, with no
    code of their own, so a scheduler cannot tell alerts from a database it cannot read (the
    line says which; the deploy templates' `|| true` swallows both); `MENDARI_PROVIDERS` stays a
    warning in the CLI and a report on a server (production runs an unscoped setting), not
    strict.
  - [x] **LP-15b** The docs' commands (#148, merged 2026-10-07). Every bash and Python block of README.md,
    docs/index.md and docs/guides/api.md carries a marker on the line before it
    (`scripts/doc_blocks.py`): `run quickstart|cli|python <group>|install|server-db`,
    `snippet <file>/<step>` (the same text as a snippet CI runs), `include <file>`, or `skip
    <reason>: <why>` from a fixed list (private-repo, docker, ehr, database, registry, secrets,
    settings, long-running, placeholder, ci; each names the CI job that covers it where one
    does). `tests/test_doc_blocks.py` fails on an unmarked block, an unknown language, a skip
    without a reason, a snippet block not equal to its step, and a landing code block that is
    not a snippet step. CI's `quickstart` job (a clean runner, no cache) runs the quickstart
    (timed, from the markers now), the CLI tour, the Python examples, a fresh `pip install .`
    and `'.[server]'`, and `check_doc_snippets.py`: a REST, a FHIR and an AI Guard decision by
    the documented commands, the Python examples, and the README's commands on the database the
    open pass's server wrote (`audit export`, `feedback export`). An AI Guard example, HTTP
    (`snippets/guard.sh`) and Python (`snippets/guard.py`, run as a program): a dose above the
    daily maximum, blocked, from the pack's own tests; in the README (replacing a sketch that
    could not run), the API guide and the /docs landing (a new "Guarding a model's replies"
    section). Docs: the server install and its extra (the CLI's hint names the clone form),
    `MENDARI_PACKS_PATH` and `MENDARI_PACKS` (README, docs home, API guide, the deployment
    settings table), and the inbox rule as LP-01 enforces it (the README says what the decide
    step prints with and without a model, quoting the reason). The CLI tour was split so the
    lines that need your data or a running server are marked as such. Review: one adversarial
    review (two lenses), each finding checked by a skeptic: 1 medium (a skip hid a runnable
    `audit verify` and said CI ran it; it now runs on the server's database) and 8 low (the
    install check passed without the server extra, now it starts `mendari serve` and waits for
    `/api/health`; a registry skip's reason overstated what CI runs; block quotes and longer
    fences slipped past the parser, and a raw `<pre>` would; the quickstart left `mendari new`'s
    packs in the clone, now removed after a run; a held draft's `fallback`; the packs-folder
    rows (precedence is by version, a missing folder is skipped); the API guide's
    `MENDARI_PACKS`; the runner's usage line), all fixed, 1 refuted.
  - [x] **LP-15c** The release paths (#149, merged 2026-10-07). `release.yml` gains `target` (`testpypi`, the
    default, or `pypi`): `publish-testpypi` publishes the tested files through its own
    protected `testpypi` environment (the same guards: dispatch from a tag, the build first, a
    public repository, reviewers checked through the API), then `verify-testpypi` downloads
    what TestPyPI serves, compares it byte for byte with the tested wheel, installs it on a
    clean runner with only its dependencies' locked, hash-checked versions from PyPI (no name
    on TestPyPI can stand in for one) and runs `mendari test`. A pre-release (`0.1.0rc1`) may
    stand under its release's changelog section. Images are built once for linux/amd64 and
    linux/arm64 into an OCI archive; each architecture's image is taken from that archive,
    tested (`mendari test`, under QEMU for arm64) and checked for excluded paths, and the
    archive is pushed as it is (`skopeo copy --all`), with a release to PyPI only. `docs.yml`
    deploys the site to GitHub Pages by hand (dispatch only, public repository only, the
    `github-pages` environment) from the strict build, checked for excluded paths and for the
    notice on every page: the docs site now shows the notice (`mendari.notices`, through a
    MkDocs hook and Material's announcement bar; `scripts/check_site_notice.py`, also in CI's
    `docs` job). `site_url`: `https://mendari-hq.github.io/mendari/`. Nothing published or
    deployed. **Owner:** the docs host is GitHub Pages at `site_url`, the default (confirmed,
    owner, 2026-11-07; a custom domain would be a `site_url` change). The organization is on
    GitHub Team, where Pages publishes from a private repository and the site is public to
    anyone; what holds it back is `docs.yml` itself, whose deploy job skips while the repository is private. The first TestPyPI
    upload no longer waits on SNOMED CT (swapped out of the packages, LP-14) but needs a public
    repository (`release.yml` publishes to TestPyPI only from one); the `testpypi` environment and
    TestPyPI's pending publisher (owner steps 7 and 8). **Review** (two lenses, each finding
    checked by a skeptic): 2 medium, fixed: the rehearsal at `0.1.0rc1` failed the release's
    own suite (the changelog test wanted an rc section; a full run at `0.1.0rc1` now fails only
    the terminology pin, which the rc PR adds), and the emulator and builder images the image
    actions pull were moving tags (now pinned by digest, and the supply-chain test reads them);
    8 low, fixed (a skopeo pull no longer counts as built here; the Pages artifact is the
    checked `site`; one docs deploy at a time; 404.html is checked and the notice check has
    tests; check_release's wording; comments); 1 refuted (skopeo negotiates Docker's API).

**Pre-public:** LP-13 and LP-14 (done above), LP-15 (above: a, then b and c), LP-03 (below).

- [x] **LP-03** Launch gates as a checked file (#150). `launch/gates.yaml`: the audit's five
  gates and their 33 prerequisites, in its order, each `build` or `owner`, each with probes
  (tests, a file, a setting, the owner's attestation, or `pending` naming the prompt). An
  attestation about packs (clinician review, the FDA opinion) names each pack's digest and is
  void once one changes. `scripts/check_gates.py` prints the table and asserts a gate only when
  asked (`--assert`); CI's `browser` job (with Postgres, so both databases' cases count)
  reports it in the run's summary and fails only on a broken gates file. Today: G1-2 2 of 6,
  G2-3 1 of 8, G3-4 0 of 7, G4-5 0 of 6, G5-6 0 of 6. Read against the audited `e55ed48c`,
  every prerequisite is unmet, the audit's 0 of each (each has a pending probe, an empty
  attestation, a missing NOTICE or runbook, or tests that did not exist yet); worked out by
  hand, as the script itself stops there with exit 2, naming the test files it lacks. A5 waited
  on LP-08's open owner question (an explicit `MENDARI_PHI_MODE=off` with sign-in starts with a
  warning outside Vercel, where A5 step 1 asks for a refusal; since closed: the refusal, LP-08
  above). New evidence tests: the notice's
  three clauses, the acknowledgement required by default (the notice tests now run on the
  default), axe on the acknowledgement dialog, the label on every dashboard view of every
  reference pack and its words, CONTRIBUTING's sign-off and the Contributor Covenant 2.1. `tests/test_shipped.py`: the Definition of
  shipped as inventories (no mock, stub, fake or TODO in product code; no vendor or model name
  outside `providers/`, with its four sign-in and library exceptions; every state-changing route
  of the engine, the registry and the sandbox payer listed with its chained event or the reason
  it has none; the package version; a licence line on every tracked source file). The dashboard
  no longer formats one vendor's model ids specially (a model reads as its id; `jev-…` still
  reads "Jev …"). `uv run pytest -m core` is the edit loop (about 20 s); CLAUDE.md says the full
  suite takes about 6 min. **Found, for later prompts:**
  - Writes with no chained event (an expected failure in `test_shipped.py` until they are):
    `POST …/evals` stores a report unchained (S6.coverage: an `eval.recorded` event, for
    LP-10); the registry's writes (every one chained since LP-18b, #229: `NOT_CHAINED_YET` is
    empty; LP-19's claims and their approval are chained as they are built).
  - The terms, privacy notice and acceptable-use pages (gate G2-3) are drafted on the public page
    (`web/terms.html`, `privacy.html`, `acceptable-use.html`, each marked a draft pending legal
    review; the owner's and counsel's questions in `notes/legal-pages-open-questions.md`).
    Linking them from the hosted site and recording acceptance have no build prompt yet.
  - LP-02's "a tag run" of the secret scan conflicts with LP-13's rule that nothing starts on a
    tag push: it runs on a schedule and in `release.yml`'s build instead (LP-02 above).
  - Collecting the tests imported `mendari.server.vercel`, which builds the app on whatever
    `DATABASE_URL` the developer's shell exports. Fixed in #151, with every other setting
    that names a live system (the AI Gateway key, LLM and Jev endpoints, the alert webhook, the
    registry) removed before collection, and no test able to mint a Vercel OIDC token
    (`tests/test_isolation.py`).

**Trust layer:** LP-10 statistics and ledger, LP-11 hidden sets and blind labelling, LP-17
scoreboard and grading, LP-16 for registry-pulled packs (community packs already run on RE2
with a CPU budget, #133), LP-18 and LP-19 registry, LP-12 accounts, LP-20 pack store, LP-21
operations, LP-22 site network, LP-23 EHR pilot.

**LP-16, Option A scoped (owner, 2026-10-07):** packs the deployment does not own run on the B11
community engine: RE2 for rules and input-schema patterns, the load-time caps, the schema keyword
allowlist, the 10,000-character input limit and the 1 s per-decision CPU budget (`time_budget`,
then a person decides). That is registry-pulled packs now and store packs from LP-20; community
packs already do, so B9 does not wait for LP-16. A pulled pack whose digest equals a reference pack
in the installed release keeps the maintained engine, so `hub-e2e`, the sandbox registry and
`mendari pull mendari/referral-router` keep working; the registry checks a publish on the same
engine (`hub/archive.py` loads it as `local` today). Reference (maintained) packs and
operator-installed local packs stay on `re`, unchanged, and LP-16's requirement 1, its first test
and gate G3-4's probe are narrowed to packs the deployment does not own. Accepted with it: on `re`,
a crafted 10,000-character inbox message can hold Python's lock for about 0.55 s, stalling its
instance (rate limits bound it). A wall-clock deadline across one decision's providers applies to
every pack. No worker process (Option B) for now. The four lookaround patterns (ai-guard,
clinical-trial-prescreener, referral-router) are rewritten, with a decision-identity proof and a
rebuild, before a reference version that differs from the installed one is published to a
registry or copied into the store.

- [x] **LP-16 build** (ADR-0009), in the PRs below (16c-2 is not part of it, below):
  - [x] **16a** (#185, merged 2026-10-08) The engine for packs the deployment does not own:
    `Pack.bounded` (a community pack, or a pulled one whose digest, the one `load_pack` hashed, is
    not a reference pack's of the installed release, `core/reference.py`);
    `archive.extract(..., origin=)` required, with `hub` from the registry's publish,
    `mendari pull` (before the rename, so a refused pack is never cached) and
    `mendari pack verify`; `Decision.from_path` takes its path's origin; `mendari pull` says which
    engine; ADR-0009, the spec amended in place, the docs, threat model D8 and S15; the a3 probe's
    pending line replaced by its tests. No pack artifact changes.
  - [x] **16b** (#184, merged 2026-10-08) A wall-clock deadline across one decision's remote
    calls, every pack (`MENDARI_DECISION_DEADLINE_S`, 120 s unless set), recorded as `timeout`;
    an answer still arriving stops at it, the CQL engine's included; threat model D9.
  - [x] **16c-1** (#187, merged 2026-10-08) ai-guard's `claim-about-the-record` without its
    lookahead, spelled out a letter at a time and exact on every input on both engines
    (`test_lookaround_rewrites`: every shipped input, the ledger, a seeded fuzz on `re` and RE2),
    ai-guard 0.1.4 and its rebuild; the parity test now skips only the three lookarounds 16c-2
    holds. 16c-1 confirmed by the owner (2026-10-08): ai-guard 0.1.4 stays.
  - [ ] **16c-2** (not needed for LP-16) pregnant, pancreatitis and new-lung-mass. The owner's
    decision (Option A scoped, above) requires these rewrites only before a reference version
    that differs from the installed one is published to a registry or copied into the store, and
    the choice between S (a structural `not_followed_by` rules key, a format change) and D (the
    data-identical rewrites, with their fuzz differences and the slowdown on `re` accepted in
    writing) is made then. Until then the trial and referral packs cannot publish a version that
    differs from the installed one: a puller refuses it, with the reason. The deferral is
    confirmed by the owner (2026-11-07, in chat: "the patent gate is clear and I approve
    everything on my part"); the choice between S and D stays open.
  - [x] **F** (#190, merged 2026-10-08, with 16f; found in the 16a review) A pack from a
    registry with a reference pack's id and a version that ties its (`version_key`, now in
    `core/spec.py`) but another digest took the reference pack's place, since the cache wins a
    version tie: it ran bounded, under the reference pack's name. Refused at load for origins
    the deployment does not own (`_shadow_problem`, from `core/reference.py` `reference_packs`),
    so at publish (422), pull (before the rename), the pull's cache hit, `mendari pack verify`
    and `Decision.from_path`; one cached before is listed under `invalid` with the directory to
    remove, and the reference pack is served. Threat model S16; S15's and a3's tests moved to
    9.9.9 with their names kept.
  - [x] **16d** (#188, merged 2026-10-08) Health does no pack work in the request: every served
    pack is built at startup and health reads that snapshot; `deployment.recorded` stays on first
    use, and a pack that fails to build is never rebuilt (conflicts 15, 16, 23).
  - [x] **16e-1** (#191, merged 2026-10-08; PR C, after A and B) Closed provider configs checked
    at load (`providers.config_problems`, each provider's `config_problems`, new `FileConfig`
    and `NaiveBayesConfig`): refused for packs the deployment does not own, `Pack.warnings` for
    owned ones (`mendari list` on stderr, the start-up log, health `pack_warnings` past the
    `read` check), `mendari validate` refuses both. An owned pack's constructors raise only on
    what they raised on before, so an unread key warns and the pack still serves (conflict 17).
    The deployment's model, caps on `max_tokens`, `timeout_ms` and effort (`core/origin.py`
    `deployment_config`, keyed on `Pack.bounded`), `state_fields` on llm and jev variants and
    members, the parity test (and an exhaustive one-change sweep beneath it), the hypothesis
    egress test, the generated JSON Schema (`docs/spec/provider-configs.schema.json`,
    `scripts/provider_config_schema.py --check`); the CQL declaration search made linear, since
    load now runs it on registry packs (conflicts 17–19, 24, 25).
  - [x] **16e-2** (#192, merged 2026-10-08; PR D, after C) The provider egress allow-list
    (`MENDARI_PROVIDER_ALLOWED_ORIGINS`, off unless set; `providers/http.py` `require_allowed`
    in `require_secure`, Jev's model list and Vercel Connect, the AI Gateway's default included;
    a set `LLM_BASE_URL` or `JEV_API_URL` off it stops startup, `providers.provider_allow_list`;
    health `provider_allowed_origins` past the `read` check), relative prefetch templates
    (`core/spec.py` `PREFETCH`, `prefetch_problems`) and bare placeholders
    (`core/templates.py` `template_problems`): refused for packs the deployment does not own (a
    not-owned pack's FHIR mapping and Task text now read at load), `Pack.warnings` for owned
    ones' prefetch and card summaries; an owned pack's Task text keeps the name check where it
    serves, and `mendari validate` holds every pack's to bare placeholders; `fill` unchanged
    (conflicts 20, 21).
  - [x] **16f** (#190, merged 2026-10-08, with F) Depth and `$ref` bounds for the input schemas
    of packs the deployment owns (`core/input_schema.py` `expansion_problems`, over
    `core/schema_graph.py`, the graph moved out of `core/caps.py` with each `$ref` edge marked;
    the caps count as before): nested at most 32 deep, chains of at most 16 `$ref`s, at most
    1,000 schema objects on one value, loops only as a recursive list or tree, and no
    `unevaluatedProperties` or `unevaluatedItems` below another (conflict 22). The shipped
    packs, scaffolds and fixtures: 6, 1 and 2. Packs it does not own keep B11's caps and budget,
    unchanged. Threat model D10.
  - [x] **16g** (#219, merged 2026-10-08; PR E, last) The threat model's "Pack content" part
    (`docs/security/threat-model.md`, between the STRIDE table and the residual risks): what a
    pack can set (every manifest input, provider type and variant key, the wording templates,
    `cds_hooks.prefetch`, the attestations), which packs the deployment owns (by origin and
    digest), and what a pack can and cannot do, each with its row or residual (conflicts 23 and
    24 are written there). `tests/test_threat_model.py` holds it to every input, to each
    provider type's closed config keys, to rows and tests that exist, and to no line that reads
    as a row; gate a3's `what` says LP-16a–g. With 16g merged, LP-16 counts as merged for LP-18
    and LP-20, 16e-2 with it (16c-2 is not part of LP-16, above).

  **LP-16 was built in this order:** A (F and 16f); B (16d, beside A); C (16e-1, after A and B);
  D (16e-2, after C); E (16g, after A to D).

  **Conflicts written before building LP-16, and the default each PR follows** (each confirmed
  by the owner on 2026-11-07, conflict 7 as the deferral only, 16c-2's S or D still open; each
  default is named by the PR that depends on it):
  1. (16a) Requirement 1 and its first test are narrowed to packs the deployment does not own
     (owner, #175). Test 1 at 100,000 characters: a pulled pack refuses text over 10,000
     characters before any pattern runs (BLOCK "input rejected"), so the test asserts the
     refusal at the decision and linear scaling at the pattern level. Reference and local packs
     stay on `re`, with no budget.
  2. (16a) The engine follows the bytes. A pulled pack runs on `re` only when the digest
     `load_pack` hashed equals one of the installed release's bundled packs'. Metadata, the
     manifest's `publisher.type` and the registry's recorded digest never choose the engine. A
     local copy of a pulled pack is owned (`re`), and there is no setting to put a pulled pack
     on `re`.
  3. (16a, 16b) Error codes: `time_budget` stays the CPU budget's (requirement 2's
     `budget_exhausted` is the daily call limit: HTTP 429, FHIR "throttled"). The deadline uses
     `timeout`. No code is added.
  4. (16a) The whole B11 engine comes along for a pulled pack, not only the five items the
     owner named: the input-shape limits (10,000 values, depth 32), the 100-error stop and the
     short schema reasons, because they share the gate. Run-time reasons stored in the audit
     keep "a community pack" for community packs and say "a pack from a registry" for pulled
     ones. Load-time messages become origin-neutral ("a community or registry pack").
  5. (16a) No policy change. "Then a person decides" is held as: a pulled pack's `time_budget`
     never leaves AUTO, and a failed primary abstains. A pulled pack's own BLOCK rule that
     matches on answers from other slots still decides, as for every failure code today. A
     BLOCK can be the safe direction: ai-guard's `possible-block` means "do not send".
  6. (16a) ADR-0007, the threat model's residual risks and `SECURITY.md` promised an isolated
     process; the owner chose no worker process. ADR-0009 records Option A scoped and amends
     them; ADR-0007 has an "Amended by ADR-0009" status line.
  7. (16c) Only the ai-guard rewrite is decision-identical on every input. pregnant and
     pancreatitis lose matches (the unsafe direction) and are 26 times slower on `re`;
     new-lung-mass differs both ways. Default: 16c-1 (ai-guard) lands; 16c-2 waits for the
     owner's choice between S and D. Until then the trial and referral packs cannot publish a
     version that differs from the installed one: a puller refuses it, with the reason.
  8. (16c-2 if S) A new rules key is a format change (Definition of shipped §8,
     `notes/vision-audit.md`). Default: not in LP-16.
  9. (16a) "Pin the dialect in the spec": 0.2 §5 is amended in place, as B11 did. The pack
     format is unchanged and every pack loads as before.
  10. (16a, 16b) Gate G3-4: the a3 `pending` probe is replaced by 16a's tests and 16b's
     (`test_a_deadline_never_automates`); its narrowed `what` holds no pending line. "Fuzzing"
     in the `what` line keeps no probe of its own.
  11. (all) Requirements 4–7 and the hypothesis, health, parity and nested-`$ref` tests are not
     narrowed by the owner and are not in 16a–16c. Default: they ship as 16d–16g: 16d, health
     does no pack work in the request; 16e, closed provider configs (for packs the deployment
     does not own, the deployment chooses model, `max_tokens`, `timeout_ms` and effort;
     `state_fields` is required on remote variants; an egress allow-list); 16f, a depth and
     `$ref` bound for owned schemas, only if every shipped pack passes, else a new conflict;
     16g, the threat model's "Pack content" part. LP-16 counts as merged for LP-18 and LP-20
     only when 16d–16g land too.
  12. (16a) The sandbox registry seeds from the host checkout into an image built earlier.
     Default: document `docker compose up --build` (README, `docker-compose.yml`).
     `hub_seed.py` is unchanged and stops at the first refusal.
  13. (F) A pack from a registry with a reference pack's id and a tying version but another
     digest is refused at load, pull and publish. A deployment that already cached one switches to
     the bundled pack and lists the cache copy under `invalid`; the registry refuses such a publish
     with 422. Default: refuse, for origins the deployment does not own only. A copy in
     `MENDARI_PACKS_PATH` (local) can still shadow, by the operator's choice (conflict 2).
  14. (F) No rule says a changed reference pack bumps its version (referral 0.1.4, inbox 0.4.0
     and prior-auth 0.2.0 each changed content without a bump). Default: no new CI check in
     LP-16. The registry's refusal (conflict 13) forces the bump at publish, and conflict 12
     already says the sandbox registry must be rebuilt (`--build`).
  15. (16d) "While a decision on it is running" cannot hold in-process for an owned pack on `re`:
     `re` holds the GIL (`(a+)+$` on 25 characters stalled a 1 ms ticker for 1,080 ms). Default:
     the running-decision half is narrowed to packs the deployment does not own; the loaded-pack
     half holds for every pack.
  16. (16d) The deployment record. Default A2: build every served pack at startup and record
     `deployment.recorded` on first use; health never writes. A pack that fails to build is
     never rebuilt, and its reason is re-raised. Startup costs about 21 ms of CPU, and about 5 to
     10 s more on the suite (240 `create_app` calls).
  17. (16e) Requirement 5 validates every variant at load. For owned packs that would refuse packs
     that load and serve today (an unused broken variant, extra keys nothing reads). Default:
     refuse packs the deployment does not own; owned packs load with `Pack.warnings` (health
     `pack_warnings`, the startup log, `mendari list`), and `mendari validate` refuses both. The
     parity test is strict for packs the deployment does not own and "refused or warned" for
     owned ones; making owned packs refuse later is a one-line change, for the owner. "Parity"
     covers provider configs and, for packs the deployment does not own, the FHIR mapping; test
     inputs and datasets stay validate-only, since they are attestations.
  18. (16e) The prompt says "caps"; conflict 11 says "chooses". Default: `model` is the
     deployment's; `max_tokens` and `timeout_ms` are capped at the provider defaults (4096; LLM
     30 s, Jev 5 s), and a lower pack value is kept; `reasoning_effort` is kept at `none` or `low`
     and removed if higher. No new settings. Community packs are now capped on timeout and effort
     too (visit-sorter sets neither). A hub pack's pinned LLM model no longer applies, so its
     remote answers can change; no shipped or deployed pack pins one.
  19. (16e) "Remote variants" means llm and jev (`MODEL_TYPES`). CQL is `remote = True` but sends
     a fixed bundle (`record_fhir.py`) and has no `state_fields`. Default: `state_fields` (at
     least 1 entry) is required only on llm and jev variants and members of packs the deployment
     does not own. Fixture changes: visit-sorter and `test_decision_deadline.py`.
  20. (16e) Egress allow-list. Default: when unset, no new check (endpoints come only from fixed
     environment names). When set, it is checked at startup for the endpoint variables that are
     set, and on every outbound provider request, the gateway default and Vercel Connect
     included (`invalid_endpoint`). CQL stays on the FHIR allow-list.
  21. (16e) Requirement-5 items not in conflict 11's list: the generated JSON Schema (PR C),
     relative prefetch and bare placeholders (PR D). Default: refuse packs the deployment does
     not own, warn owned ones. Their Task-text templates are now checked at load; `fill` is
     unchanged.
  22. (16f) Default: the bound applies to owned packs only; packs the deployment does not own keep
     the caps and the CPU budget ("killed within the budget"), so B11's budget tests are
     unchanged. Every shipped pack, template and fixture passes, with margins of at least 5x on
     depth, 16x on the chain and 500x on applications. An operator's local pack beyond the bound
     stops being served and is listed under `invalid`, as conflict 11's default says. Recursive
     schemas are allowed only as simple cycles through properties or items; a binary-tree
     recursion is refused, and so (PR A) is a loop that leads to another loop, since one value
     would meet the inner one once for each level of the outer. The PR A review found that
     `unevaluatedProperties` and `unevaluatedItems` validate again every schema below them
     (jsonschema's `find_evaluated_*_by_schema`), so nested ones multiply the work within every
     count above (on `{"a": 1}`, one `$defs` of 13 took 7 ms, two 3.5 s, three over 60 s).
     Default: refuse a schema holding one that leads, through any applicator (a property's or an item's too, which also
     multiplies), to another holding one, or back to itself; one per path still loads. No shipped
     pack, scaffold or fixture uses either keyword.
  23. (16d, recorded only) Health and `admit` share AnyIO's 40-thread pool with the decide
     routes, which can wait up to `MENDARI_DECISION_DEADLINE_S` (120 s). Default: a residual
     risk, written in 16g; not in LP-16.
  24. (16e, recorded only) These still run as packs set them, for packs the deployment does not
     own: Jev `cost_per_1k_input_tokens_usd` (which sets the recorded `cost_usd`), `batch: false`,
     `temperature`, `JEV_DEFAULTS` and unscoped `MENDARI_PROVIDERS` settings applying to hub packs
     by id, and the eval route letting a caller choose any variant of a pack that is not a
     community pack. Default: a residual risk (16g), for LP-20; changing them would change hub
     packs' decisions.
  25. (16e) `hypothesis` is added as a development dependency (`pyproject.toml`, `uv.lock`) and
     runs with `derandomize=True`, so CI stays deterministic.

**B38, the pilot EHR (owner, 2026-10-07):** Epic, with Oracle Health as the backup, chosen by
vendor fit while no design partner is on record; the first pilot site's own EHR overrides it.
LP-23 builds to Epic's published CDS Hooks token (RS384 or ES384, a `jku` equal to an allow-listed
URL, `iss` the FHIR base, `aud`, `sub`, `jti` replay) with a test key pair. The owner signs up for
Epic's free sandbox when LP-23 starts, from a checklist Claude sends (the APIs the packs read,
ServiceRequest and Communication included). An Epic shared by several organizations (Community
Connect) is not supported yet.

- [ ] **LP-23** EHR pilot readiness (A12; ADR-0014, Accepted 2026-11-06). Built but for the
  owner's run (2026-11-06, the LP-23 lane; 23j, once the owner confirmed the referral `show` list
  on 2026-11-06, in #281; 23k, once the owner gave the go-ahead on 2026-11-06, in #282): the
  Epic sandbox run, which is the owner's, and the clinician's note on the MedicationRequest rule
  that 23k adopts (B21; answered by the owner, a physician, on 2026-11-07: acceptable with
  changes, which a build PR makes next). Gate G5-6's `a12-ehr-pilot` names the Epic run as
  pending, and the owner's lines below are what is left. The scorecard's after grade,
  at the close-out commit, is overall level 1, as before (failure behaviour, on the pilot's
  `MENDARI_FHIR_VALIDATION`), and 23j's CW-3e puts cards and workflow at level 3. Planned by a
  mapping workflow
  (2026-10-17, against main `06d5332f`) as thirteen PRs, 23a to 23m, each safe alone; the full
  plan, its 60 conflicts with the default each PR follows, the owner decisions and the risks are
  in `notes/lp23-plan.md`. An EHR calls the CDS Hooks services with its own signed token, and
  Mendari reads EHR-shaped data and decides, or records what it could not read, abstains and
  still answers. Off until the owner acts: with `MENDARI_CDS_CLIENTS` unset no JWT is accepted
  anywhere and sign-in behaves as today; with `MENDARI_SITE_CODES` unset no code is translated;
  `MENDARI_CDS_SANDBOX` is set only by the compose sandbox engine and the test suite; the trial,
  prior-authorization and referral packs are on spec 0.3 since 23k; Production gets none of these
  settings until a pilot is signed. No database table is added. LP-23 computes no statistic and ranks nothing: the
  scorecard shows rubric levels with their scope, a data-coverage record counts per field for one
  decision, and Inferno results are each test's own outcome.
  - [x] **23a** (#256) The plan (`notes/lp23-plan.md`), ADR-0014 (Proposed, amending
    ADR-0006's rule that the narrow EHR principal is for SMART launches only), the Epic sandbox
    checklist B38 says Claude sends (`notes/epic-sandbox-checklist.md`), the EHR scorecard
    (`notes/ehr-scorecard.md`: rubric v1, seven dimensions, graded at `06d5332f` with LP-23a;
    overall level 1, from who may call from where, failure behaviour, and cards and workflow;
    `tests/test_ehr_baseline.py` shows today's sign-in on the CDS services, an EHR's own token
    refused, and the HL7 validator and smart-launcher-v2 CI jobs), synthetic FHIR for the trial and
    prior-auth packs (`sandbox/fhir_records.py` `trial_entries`, `prior_auth_entries`: US Core
    6.1.0, deterministic, no SNOMED CT, deciding as their records do), and a golden of today's
    FHIR decisions (`scripts/fhir_decisions_golden.py`, `tests/fixtures/fhir/decisions-golden.json`:
    every case of the four mapped packs' local-site examples, 1,150 cases; 41 prior-auth orders
    with no requirement set are refused through FHIR, as today). Nothing changes how a pack decides.
  - [x] **23b** (#262) Signed CDS client tokens: the registry, the verifier and the `cds`
    principal (one adversarial security review). `server/cds_clients.py`: `MENDARI_CDS_CLIENTS`
    checked at startup (entry and field named, never a value or key material), keys by value or
    one allow-listed `jwks_uri` (`JwksCache`: an hour, an unknown `kid` refetches at most once a
    minute per client on each instance, one fetch at a time, a private key keeps nothing), and
    `verify` (8 KiB, the header `alg` `kid` `typ` `jku` only, RS384 or ES384, `jku` exact, one
    key per `kid` and type, `aud` the public URL plus the path, `exp` `iat` `jti` required, 330 s
    at most, a pinned `sub` and `tenant`, then the guard's organization and permission checks,
    the `jti` claimed last as `cds-jti`, each refusal one of 17 reasons).
    `server/auth.py`: the CDS branch above open mode's shortcut, a `Bearer` credential holding a
    `.` on `/cds-services` only; kind `cds` (`cds:<id>`, `cds <id>`) holding discover and decide,
    refused anywhere else (`cds_only`). `cds_invoke` refuses an unregistered `fhirServer` with
    `403` before charging or fetching; health lists `cds_clients`; `smart_claim` purges up to
    100 expired rows. `Signer`, `assertion` and `forged` moved to `tests/jwt_signers.py`. Tests:
    `tests/test_cds_clients.py`, `tests/test_cds_auth.py` (SQLite and Postgres), the route matrix
    and the per-kind cap extended with kind `cds`. Threat-model rows S21, D13 and E15, with S5, E1
    and R2 updated; `docs/guides/connecting-an-ehr.md`, `SECURITY.md` and the review packet.
    The review's fixes: a token refused for its organization's record or the route's permission
    claims no `jti`; a JWK Set of the wrong JSON shape (`check_jwks`) and a JSON answer nested too
    deep (`GuardedHttp`) are refusals, not `500`s; an integer `exp` past a float's range is
    `cds_claims`; the session table keys a lone surrogate (`_session_key`, every older key
    unchanged); `cds_issued_at` folded into `cds_claims` and `cds_not_yet_valid`. Open mode still
    answers on any host until 23c.
  - [x] **23c** (#269) Open mode answers CDS Hooks on this machine or under the sandbox flag
    only; CORS for registered origins. `server/cds_clients.py`: `on_this_machine` (a loopback
    `client_ip`, IPv4-mapped included; no `Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host` or
    `X-Real-IP`; one `Host` naming this machine), `CdsOpenMode`, and `cds_sandbox`
    (`MENDARI_CDS_SANDBOX`: `1` or `0`, `1` stops startup on Vercel or with sign-in on, with a
    warning when on); `CdsRegistry.origins` and `from_its_origin`. `server/auth.py`: with sign-in
    off, `/cds-services` from elsewhere is a `403` (`open_mode_remote`) before any credential is
    read, a verified token's too; a `cds` token from a page on an origin neither its client's nor
    this site's is a `403` (`cds_origin`) before its `jti` is claimed. `server/cors.py` (new):
    on `/cds-services` only, the registered origins' preflight answered before the guard,
    `Access-Control-Allow-Origin` on their answers, `Vary: Origin`, never credentials.
    `server/origin.py`: a registered origin passes on `/cds-services` only, without a Mendari
    cookie (`is_this_site` shared). Only the compose sandbox's `engine` sets the flag
    (`tests/test_deploy.py`); the suite runs as if it were set where it may be
    (`tests/conftest.py` `_cds_sandbox`, which never sets the variable, so no subprocess server
    inherits it), and `tests/test_cds_open_mode.py` takes that away. The API examples gain
    `cds.sh` (discovery and the inbox's hook: README, the API guide, `/docs`, and the
    `cds-hooks` preset's CI run). Tests: `tests/test_cds_open_mode.py`, `tests/test_cds_cors.py`
    (SQLite and Postgres), `tests/browser/test_cds_cors.py`; the route matrix with open mode off
    this machine, and the origin inventory with a registered origin. Threat-model rows I16 and
    E16, E6 updated; the scorecard's WC-2b, WC-3a and WC-3b are met (who may call from where at
    level 3; overall still level 1). Beyond the plan's letter: `X-Forwarded-Host` and
    `X-Real-IP` count as forwarding headers too, and a registered origin passes the cross-site
    check only without a Mendari cookie.
  - [x] **23d** (#264) A hook that always answers: an unreadable record gives a recorded ABSTAIN
    with its reason, and every FHIR decision carries its data-coverage record. Each prefetch key
    is read on its own (`cds_hooks.read_each`, shared with the SMART launch's decide); a key that
    could not be read, an OperationOutcome, a fetched resource that is not R4 (`not_r4`), and a
    note that is only a link or does not decode make the decision ABSTAIN before any provider,
    with `could not read <key>: <reason>` from the closed set `errors.ReadReason` (13 codes, set
    at each raise in `fhir_client`); refused input still BLOCKs; the hook answers `200` with an
    `info` card whatever `show` says; neither prefetch nor `fhirServer` is a `412`
    (`PrefetchRequired`); a null value is no data and is not fetched; CRD gets `indeterminate`
    for a `technical` reason when the Coverage was read (`davinci.indeterminate`, a fourth CRD
    sample). `standards/data_coverage.py` (`mendari.data-coverage/1`) is in every FHIR
    decision's record, and in the answers of `fhir/decide` and the SMART launch's decide (a card
    and `$decide` give the decision id); null on JSON ones. Threat-model row I17; the
    scorecard's FB-3a to FB-3c are met (failure behaviour stays at level 1 on FB-2d). The 23a
    golden is unchanged. Every value the client sent goes through the structural R4 check
    before any is set aside (a `400`, as before), and a key sent empty (`{}`, `[]`, `""`) still
    counts as left out. Beyond the plan's letter: a declared key whose value is otherwise not a
    resource (a string, a list, an object with no `resourceType`) is `unsupported_type` (it was
    read as no data, or a list as its resources), while a required resource it stands in for is
    still a `422`; a decision's search past `MAX_PAGES` is `cap` (it was cut short without a
    word); a search page whose `entry` or `link` is not R4's shape is `not_r4` (a `500`
    before), and a later page that is not a Bundle is its reason (it was read as the first
    pages). The section for `docs/guides/connecting-an-ehr.md` waits for 23b, which creates
    that page.
  - [x] **23e** (#280) A vendor-shaped synthetic profile, and a signed sandbox CDS client over
    HTTP (the first end-to-end result). `sandbox/ehr_profile.py`: five synthetic patients for
    each reference pack with a standard hook (trial `patient-view`, prior auth and referral
    `order-sign`), each made from a local-site case by `fhir_records` and reshaped after Epic's
    and Oracle Health's public FHIR documentation: DocumentReference → Binary (text/plain,
    text/html, one RTF to be unreadable), Conditions coded by a fictional site system and
    ICD-10-CM in either order, MedicationRequest → Medication (RxNorm) with a dosage
    `boundsPeriod`, `Coverage.payor` → Organization with a site identifier, referrals under a
    site order type; the profile's own ids, no sandbox tag, no SNOMED CT; its two site code
    systems published with the sandbox's terminology. `fhir_workflow.seed` seeds the trial and
    prior-auth packs and `ehr-profile` too (a pack already there is found by its first case's
    Patient, the referral router's included). `sandbox/cds_client.py`, `mendari sandbox
    cds-client`: an EC P-384 key in memory, its JWK Set served, its registry entry printed, then
    discovery and every standard hook per patient with a new ES384 token (`jku` set), no
    prefetch; exit 1 on anything but a `200`, a hook answer that is not CDS Hooks', or a card
    saying a read failed (anything unread but a linked note's `notes: not_fetched`). Compose:
    both engines register it (`http://cds-client:8300`, on their allow-lists; the open engine
    too, since without a registry a JWT is a `401` in open mode as well, its registry
    empty-able for a public URL of your own); the service `cds-client` (profile `signin`) seeds
    and runs it against both (`--audience` the open engine's own public URL setting);
    `sandbox-e2e` runs it last. Server change: `MENDARI_PUBLIC_URL` may be plain http for any
    loopback or single-label host (the engines' `http://engine:8000`,
    `http://engine-signin:8000`), one check for the CDS registry and Backend Services
    (`origin.checked_public_url`, S21). Scorecard: WC-4a and CE-3a met (who may call from where
    to level 4, conformance evidence to 3; overall still 1); DS-4a partial until 23k, since the
    referral router makes no decision for ehr-ref-1, 3 and 5. `tests/test_ehr_profile.py` pins what
    each pack cannot read yet (abstains on a linked note; no referral found, or a diagnosis read as
    the site's code, when the site's coding comes first; no MedicationRequest; no payer reference),
    each with why; 23k takes entries off it when the shipped packs move to spec 0.3.
  - [x] **23f** (#270) Cards and card feedback as CDS Hooks 2.0 and Da Vinci CRD expect.
    `cds_hooks.card`: the card's `uuid` is the decision id and its suggestion's
    `uuid5(UUID(id), "suggestion")` (`suggestion_id`); `selectionBehavior: at-most-one`; three
    neutral `overrideReasons` (owner decision 8) from the new CodeSystem
    `https://mendari.co/fhir/CodeSystem/card-override-reasons`, defined in `terminology.py`
    (served, pinned, written by `scripts/fhir_terminology.py` for `fhir-conformance`). Summaries
    from the card-only `templates.fill_display` with `Question.name_of` (the default verb table
    too; `fill`, Task text and performer codes unchanged), every expansion checked to be under
    140 characters at load, as CDS Hooks 2.0 says (at most 139, `spec.SUMMARY_CHARS`, through
    `spec.card_summary_problems` and `templates.longest`, in `manifest_template_problems`:
    refused from a registry, warned when owned, refused by `mendari validate`; an owned pack's
    card cuts it to 139). The detail is the status, the notice and one plain sentence; the
    confidence is gone in every configuration (without `MENDARI_PUBLIC_URL` the reasons stay,
    but for the confidence thresholds', `policy.threshold_reason`). With `MENDARI_PUBLIC_URL`: an
    `absolute` link to
    `/?pack=<pack>#audit/<id>`, the reasons behind it, `source.url` the pack's card; a `smart`
    link to `/smart/launch?pack=<pack>` only with SMART on and the `fhirServer` a
    `MENDARI_SMART_EHRS` base of the caller's organization (`CardLinks`). Discovery declares
    `davinci-crd.version` `["2.2"]` and the `coverage-info` option for packs with
    `coverage_information`; `coverage-info: false` sends neither cards nor system actions, and
    still decides. `POST /cds-services/{service_id}/feedback`: a `decide` rule before the
    catch-all, DECIDE in `_ROUTE_SCOPES`, the `cds-hooks` preset's calls; a closed body
    (`cds_hooks.CardFeedback`: 1 to 50 cards, each once, accepted with the card's suggestion or
    overridden with one of its reasons, an aware `outcomeTimestamp` with a time in UTC, CDS Hooks
    2.0's Coding (`system`, `code`, `display`), `extension` on any object, never kept;
    `userComment` dropped); all
    or nothing in one transaction (`AuditStore.record_card_feedback`): the service's pack, the
    caller's organization and, when the caller is named, its chained `requested_by`, else one
    `404` (`UnknownCard`); the decide bucket, no daily cap. `card.feedback` joins
    `DECISION_KINDS`, `audit_fhir._EVENTS` and the audit-event CodeSystem (its pin moved); it
    names its organization on every database (it is not one of `BOUND_WHEN_MARKED`). The audit
    view's data-coverage panel ("What it read", counts by field and reason; tokens only, no new
    CSS). Threat-model rows R5 and I18; the scorecard's CW-2d and CW-3a to CW-3d are met, so
    cards and workflow is at level 2 (CW-3e waits on 23j; the overall stays level 1). Tests:
    `tests/test_cds_cards.py`, `tests/test_cds_feedback.py` (SQLite and Postgres), a browser
    case with axe and the bare-percentage scan, and the route, scope, limit, preset, chained-event,
    two-organization and leak-sweep inventories. Beyond the plan's letter: `source.topic`
    (`coverage-info`) is set on a CRD service's cards, since CRD 2.2.1's base response profile
    requires it (checked at the IG's 2.2.1 tag), and so `coverage-info: false` drops those cards
    too, as CRD says; the audit link carries `?pack=`, so the dashboard opens on the decision's
    pack; each `card.feedback` names who sent it (`reporter`). For the owner: conflict 30 (the
    confidence leaves the card in every configuration) and conflict 31 (without the public URL
    the reasons stay in the detail) are both kept by leaving out the one reason that gives the
    confidence and a threshold as figures; the audit trail keeps it. Both confirmed (owner,
    2026-11-07; the owner's lines below).
  - [x] **23g** (#271) Mapping syntax 0.3 (engine and spec; no shipped pack adopts it).
    `docs/spec/decision-pack-0.3.md` changes only the FHIR mapping (`spec_version: "0.3"`, a
    nav entry, a pointer from 0.2): the `[system=<uri>]` predicate for a declared system,
    fallback `paths`, one `follow` hop resolved inside the record and never fetched,
    `attachment_text` (text/plain, and text/html through `html.parser` with scripts and styles
    dropped; its reasons `unsupported_type`, `undecodable`, `not_fetched`), `code_sets` and a
    `code_set` selector that takes a site's translations (nothing passes them until 23i), list
    `resources`, and `medication_period` by conflict 39 (B21 clinician review still owed, and the
    trial card's failure modes say it when 23k adopts it). A 0.1 or 0.2 pack with a 0.3 key is
    refused at load wherever it comes from, naming each key; a pack the deployment does not own
    (and every pack in `mendari validate`) has at most 4 paths a field and 200 codes a code set,
    and a `follow` is one hop for every pack, resolved once a field by a lookup in the record. `FhirAdapter.looked_for` gives what each field and list
    looks for (paths, declared systems, the pack's own codes) from the mapping alone, for 23d's
    `data_coverage.measure`, and `read_attachment` the reason an attachment was not read. The
    golden is unchanged, and the four mappings written with `paths` and `resources` read every
    case alike (`tests/test_fhir_mapping_03.py`, `tests/test_spec_03.py`). Merged with 23d:
    `index` keeps `excused` on the new `Indexed`; the mapping is read through public
    `Reading`, `read_field` (each path read, a `follow` resolved once a field) and `list_item`
    (one resource of one source type), which `to_input` and `data_coverage` share; and
    `data_coverage.measure` counts a 0.3 mapping against `fhir.looked_for` (the adapter's
    `looked_for`): its code set's codes, the systems it selects on (else the declared ones for a
    coded path, as 0.2), the values and codings at the paths it read in order until one gave a
    value, a `follow`'s references and what they reached, a list of several types under
    `sources`, and an `attachment_text` note that cannot be read by `read_attachment`'s reason,
    which abstains as a `base64_text` one does. A 0.1 or 0.2 pack's record is byte for byte 23d's
    (its paths, as the mapping says them, and its systems, the declared ones, kept), checked over
    every golden case with failed reads and broken notes;
    `tests/test_data_coverage.py::test_a_0_3_mapping_is_counted_against_what_each_field_looks_for`
    and `::test_an_attachment_text_note_that_cannot_be_read_abstains_with_its_reason`.
  - [x] **23h** (#280) Linked notes, payer and medication, read under the EHR's FHIR server.
    `standards/linked.py` (`Linker`): on a CDS hook with a `fhirServer` (`cds_hooks.linker`, its
    client opened when a link first needs it, the request's token) and in the SMART launch's
    decide (`smart_routes.decide_for`, the session's client; a `401` still ends the launch),
    `decide_fhir(links=)` reads what a spec 0.3 mapping reaches by a link the record does not
    hold, a `follow`'s reference (`fhir.FieldRead.missing`: `Type/id`, relative or ending an
    absolute URL, of a type the field's paths read) and an `attachment_text` note's url
    (`fhir.waiting_link`), field by field in the mapping's order, a field's paths until one gives
    a value. Each is one `GET` of one resource (`FhirClient.read_resource`, no page followed;
    `FhirClient.url_of`, the client's base check made public) of the type the field reads, a
    Binary as FHIR JSON only, the answer R4 and of that type; at most `MAX_READS` (20) a decision,
    then `cap`; at most `MAX_BYTES` (1 MiB) each once decoded, a resource's answer stopped as it
    arrives (`GuardedHttp.get_json(max_bytes=)`) and a Binary's at twice that, its content then
    measured (`too_large`); each URL once a request, whatever its spelling, so an order hook's
    draft orders share what was read; an absolute URL under the base to a resource the record
    holds is that resource, with no read. A link elsewhere (its scheme in any case) is
    `off_server`, never sent; a hook whose `fhirServer` is off the allow-list is a `422` for a
    pack that reads links (`fhir_client.check_allowed` in `cds_hooks.linker`), before anything is
    decided, nothing fetched or recorded, as conflict 14 has it for the prefetch, whether or not
    the record holds every link. A MedicationRequest its list leaves out as entered in error
    (`fhir.listed`, conflict 39) is not read for its links and nothing it could not read counts;
    a note's url to another type, relative or absolute, or to a Binary with no content is
    `unsupported_type`. What was read joins the record by its link only (`Indexed.link`,
    `resolve`), and has its R4 and US Core issues recorded, by type alone in the stored record
    (refused under `enforce`); a link not read keeps its reason (`Indexed.unread`), which
    `read_attachment` gives for a note and `data_coverage.unreadable_links` for a follow, so the
    decision abstains (`could not read <field>: <reason>`) and the field's record counts it.
    `fhir/decide`, `$decide` and `mendari decide --fhir` pass no linker: a link there is
    `not_fetched`. The 23g merge's notes, done: a followed reference or a Binary url that is not
    read now counts by its reason and abstains (the 23g test of the 0.3 record now counts its
    `Medication/none` as `not_fetched`), and a note whose Binary is read counts as read. **The
    `unsupported_type` abstain is narrowed, not confirmed:** where a path reads renditions (a
    DocumentReference's `content`, a DiagnosticReport's `presentedForm`, which R4 makes one document
    in several formats: `fhir.renditions`, `fhir.unread_attachments`), an attachment that cannot be
    read counts for nothing once another of the same resource was read as text, and no other
    rendition is fetched; an RTF-only note still abstains
    (`test_html_is_read_as_text_and_rtf_abstains`), and so does any other attachment (a
    Communication's two payloads are two contents). Tests: `tests/test_fhir_linked.py` (the plan's
    eight, and a failed read's reasons, an off-list server, a request its list leaves out, a note's
    url to another type or an empty Binary, a linked resource under `warn` and `enforce`, the SMART
    decide, the 0.2 packs and the record's shape). Threat-model row D14 (linked-read volume), E1 and
    the Pack content `inputs.fhir` rows updated; spec 0.3 §2.3 (links, linked reads) and §2.4 (their
    reasons, renditions); `connecting-an-ehr.md` gains "Reading the record" and "Linked data" (and
    drops the stale "not built yet" line on 23d); the scorecard's DS-3b and DS-3c stay missing until
    23k adopts the keys, as DS-3a does. No shipped pack is on 0.3, so no shipped decision changes
    and 23e's pinned table has nothing new from 23h. Beyond the plan's letter: an answer over the
    size is `too_large` (the closed set's reason for a size cap), where the plan said `cap`, which
    stays the count's; the payer's identifier by system is the 0.3 mapping's own path
    (`Organization.identifier[system=…]`), and the site map step of conflict 41 is 23i's. For the
    owner: no deadline spans a decision's linked reads (each keeps the client's 10-second timeouts),
    and the bound is a decision's, not a request's: an order hook of 50 draft orders can make up to
    1,000 linked reads, each answer (at most 1 MiB, 2 MiB for a Binary) kept until the request ends,
    as the residual D14 states. A request-wide read and byte budget is not built, and not wanted
    for now (owner, 2026-11-07).
  - [x] **23i** (#280) A site code map per organization, versioned, hashed into each decision,
    never automated. `core/site_codes.py`: `MENDARI_SITE_CODES`, inline JSON or a file, `{org:
    {version, sets: {<code set>: [{system, code, as}]}}}` (`default` alone with sign-in off; 100
    organizations and 10,000 translations a map at most; a key given twice is refused), read and
    checked at startup in `create_app` against the code sets of every pack found, served or not, so
    one setting serves every per-pack image (a set no pack declares, or an `as` that is not a code
    of its set, stops the server; the message names the organization, the code set and the position,
    never a system or a code). Its canonical form (`mendari.site-codes/1`: format, organization,
    version, each set's translations sorted by system and code, sorted-key ASCII JSON) and its
    SHA-256. `Decision(site_codes=)` and `site_map()`: `decide_fhir` reads with the map of
    `tenancy.current()` (the default organization in open mode) through `FhirAdapter.read`, which
    also says which code sets a translation gave a value through (`MappedInput.translated`, from
    `fhir.picked`); `data_coverage.measure` reads with the same translations (the 23g merge's note);
    `DecisionResult.site_codes` (`{version, sha256}` or null) is in `to_dict` and the chained record
    beside `data_coverage`; health shows the caller's. `core/policy.py`: the `site_code` cap, AUTO
    to HUMAN_REVIEW with `site code translated (<code set>): a person decides`, after the other caps
    and before a hold, and a rule's BLOCK to HUMAN_REVIEW, which nothing lifts (governance
    included). `MENDARI_SITE_CODES` joins `LIVE_SETTINGS`, with a §8 row and a go-live line in
    `docs/deploying-with-phi.md`. `./scripts/build-reference-pack.sh` was rerun for the policy
    change: data, models, ledgers and the scoreboard page are byte-identical, and benchmarks and
    cards differ only in the run block (id, time, latency), so nothing regenerated is committed.
    Threat-model row T9, E3 updated; the scorecard's DS-3e is met (data shapes stays at level 2).
    Tests: `tests/test_site_codes.py` (SQLite and Postgres). Docs: `connecting-an-ehr.md` (Site
    codes, fictional codes only), the spec 0.3 document, the API guide, architecture. Not done:
    `mendari decide --fhir` and `mendari bulk dataset` read no site code map (the command line acts
    for no organization; now said in the guide, the §8 row and `site_map`); no reference pack
    declares a code set until 23k. Review fixes: a field reads all its paths for the set's own code
    before a translated one counts (`fhir.read_field`, as spec 0.3 §2.5 says); each set's
    translation table is built once per reading (`Reading.translations`; 301 conditions with 10,000
    translations went from about 11 s to 0.06 s); a pack rule's BLOCK on a translated input goes to
    HUMAN_REVIEW too, as `blocks_to_person` does (T9).
    Lane merge: `tests/test_ehr_profile.py` reads the profile's site diagnosis codes through a map
    and pins the HUMAN_REVIEW, and `tests/test_fhir_linked.py` a linked Medication's
    (`::test_a_linked_resource_is_read_through_the_organizations_site_code_map`). Conflict 41's
    site-map step for the payer is not built: a map translates codings only (spec 0.3 §2.5), and
    a payer's identifier is not one, so how a payer field reads a site's payer id is 23k's (the
    owner's question in #280), answered as built on 2026-11-07 (the owner's lines below).
  - [x] **23j** (#281) Pack cards: the inbox's card template, referral cards only when action
    is needed. **The owner confirmed the `show` list on 2026-11-06** ("Confirm the default": owner
    decision 4, `[return_for_info, urgent_review]`). `packs/patient-inbox-router` 0.4.3 gains a
    `cds_hooks.card` block (conflict 45): an indicator per team (`info` for the front desk, a
    medical assistant and billing, `warning` for a nurse and a physician; a possible emergency stays
    `critical`, priority stat), a `none` summary ("Message: no suggested team; it stays in the
    shared inbox for manual triage", for an ABSTAIN and for a BLOCK on refused input alike, whose
    summary said "Blocked; to manual triage" before: the detail still says which) and its
    suggestion's label ("Create a routing task"); a team's summary keeps the engine's words with
    display names ("Routed to", "Review: route to"), every decision still shows a card, the hook
    stays `mendari-message-received` and off Inferno's list. `packs/referral-router` 0.2.1 gains
    `show: [return_for_info, urgent_review]` (conflict 46): of the golden's 300 referrals, 235 show
    a card and 65 (53 to schedule, 12 for a coordinator) are decided and audited with none, and so
    is a referral refused as invalid input (BLOCK, the `none` key: no card, and not in the review
    queue); an ABSTAIN because part of the record could not be read still shows its `info` card
    (owner decision 6, unchanged), and one whose input is also refused is a BLOCK, with no card. The
    referral pack's out-of-scope line says only a referral to send back or to review today shows the
    suggested specialty. Both versions are PATCH bumps (CONTRIBUTING.md's rule, read as the packs'
    changelogs have, 0.4.1 and 0.1.3 among them: what the cards say, with every decision identical).
    Both changelogs, the root CHANGELOG, `connecting-an-ehr.md` (The cards) and README.
    `./scripts/build-reference-pack.sh` rerun: data and models byte-identical; the two packs'
    benchmarks and ledgers identical but for the run block, the version and the digest (critical
    labels unchanged: inbox `urgency=urgent` 23 of 23, referral `urgency=urgent` 113 of 113 and
    `next_step=urgent_review` 124 of 124, none automated); their cards, benchmarks, ledgers and the
    scoreboard page (docs/grading/scoreboard.md, web/index.html) committed; the other three packs'
    differ only in the run block and are not. The FHIR decisions golden is unchanged
    (`scripts/fhir_decisions_golden.py --check`). The scoreboard: the 9 stored results of the two
    packs (6 inbox, 3 referral) read "stale: pack changed"; the check against main passes (21
    results recomputed from their inputs, their ledgers not re-run), as
    `docs/operations/scoreboard.md` says a pack change does, and none is run again here
    (`./scripts/build-scoreboard.sh` refreshes them, in this pull request or a later one). The
    registry's reference index reads each bundled pack's new version and digest. Gate G5-6's
    `a12-ehr-pilot`: the 23j pending line replaced by its tests; the scorecard's CW-3e is met,
    graded again after the close-out (cards and workflow at level 3; overall still level 1);
    ADR-0014's status lists 23j. Tests:
    `tests/test_inbox_pack.py::test_the_inbox_card_has_its_own_template`,
    `tests/test_referral_pack.py::test_only_referrals_that_need_action_show_a_card`,
    `tests/test_registry_engine.py::test_the_reference_index_is_each_bundled_packs_id_version_and_digest`
    (unchanged, passing on the new versions),
    `tests/test_ehr_scorecard.py::test_a_row_graded_again_after_the_close_out_says_so`; from the
    review:
    `tests/test_inbox_pack.py::test_a_message_with_no_team_has_one_summary_and_its_detail_says_why`,
    `tests/test_inbox_pack.py::test_the_landing_pages_example_is_this_versions_decision` (the
    landing page's example names 0.4.3),
    `tests/test_referral_pack.py::test_a_referral_refused_as_invalid_input_shows_no_card`,
    `tests/test_ehr_scorecard.py::test_a_test_renamed_after_the_close_out_had_its_old_name_at_both_commits`
    (`test_order_sign_gives_one_card_per_referral` is now
    `tests/test_referral_pack.py::test_on_order_sign_only_a_referral_gets_a_card`, cited so by
    CW-2b; the scorecard's summary row for cards and workflow says CW-3e was graded again); changed
    deliberately: `tests/test_fhir_lists.py::test_the_inbox_card_wording_is_unchanged` and
    `tests/test_cds_cards.py::test_card_summaries_use_display_names_and_task_text_is_unchanged` (the
    inbox suggestion's label).
  - [x] **23k** (#282) Pack mappings for real EHR data, on spec 0.3. **The owner gave the
    go-ahead on 2026-11-06** ("go ahead do it, full consent": owner decision 5); the clinician's
    note on the MedicationRequest rule is still owed (B21: the brief for the reviewer,
    `notes/clinician-review-medication-period.md`, says on its first line that it is not
    clinically reviewed, and gate `clinician-review` is unchanged). `clinical-trial-prescreener`,
    `prior-auth-preflight` and `referral-router` 0.3.0 (MINOR bumps, CONTRIBUTING.md's rule: MINOR
    for a change in behaviour, here what each decides through FHIR), `spec_version: "0.3"`, their
    `fhir/mapping.yaml` rewritten, logic files unchanged. Trial: a Condition by ICD-10-CM, then the
    fictional condition code, wherever the coding sits, and the first coding only when it has
    neither (the trial input schema requires a condition's and a lab's `code`, and the logic
    files invite a site to list its own codes, so a strict by-system read would have refused a
    site-coded record as invalid input, a BLOCK); a lab by LOINC, else its first coding; a
    MedicationStatement's drug by RxNorm; medications from MedicationRequest too (prefetch
    `medication_requests: MedicationRequest?patient={{context.patientId}}`), its drug through
    `medicationReference` to the Medication, else its own coded concept, its start and stop by
    `medication_period` (spec 0.3 §2.7); notes through `attachment_text`. Prior authorization: the
    order (and `focus_where`), diagnoses and procedures by their code system, else the first
    coding; the payer through `follow: Coverage.payor[0]` to `Organization.identifier` in a new
    fictional payer identifier system the pack declares, else the payor's own identifier as
    written; notes through `attachment_text`. Referral: `focus_where` through code set
    `referral_category` (FR101; a code set's name is lower case with underscores, so not the
    plan's `referral-category`), and the checklist's document and report codes as code sets
    `checklist_documents` and `checklist_reports`; labs as before. Each card's known failure modes
    state the medication derivation (the trial card states the rule and that it is not yet
    clinically reviewed; the other two say no medication is read), the by-system reading, and the
    notes. **Engine additions the mappings needed** (spec 0.3 is a draft, in no release): `focus_where:
    {path, code_set}` (`FocusFilter.code_set`; `FhirAdapter.is_focus`, `focus_resources` and
    `index` take the organization's site codes, which `decide_fhir`, the order-sign hook and the
    SMART focus list pass; a focus found only through a translation adds its set to
    `MappedInput.translated`, so the `site_code` cap applies; the data-coverage record names the
    set's codes), and after a `follow` a path of the field's own type (`fhir.own_type`, the list's
    type or the `follow` path's) read where the field is, so a field falls back from what its
    reference reaches to its own resource (`followed_types` keeps it out of what counts as a
    link); both in spec 0.3 §2.3 and §2.5, `authoring-a-pack.md` and the threat model's Pack
    content row. **Conflict 41's site-map step for the payer is not built:** a site map
    translates codings only, and how a payer field reads a site's payer id was the owner's
    question, answered as built on 2026-11-07 (the owner's lines below): a site's own payer id
    reads as `other` (`not_checked`). The FHIR decisions golden (`scripts/fhir_decisions_golden.py`):
    every trial (150) and prior-authorization (300, 41 refused) case is as before, input hash
    included; the 300 referral decisions are as before, and 162 change their input hash only,
    each because a document coded LOINC 34117-2 (a history and physical, which the checklist
    never counted) is no longer carried with its code; no decision is newly AUTO or BLOCK.
    `./scripts/build-reference-pack.sh` rerun: data and models byte-identical; the three packs'
    benchmarks and ledgers identical but for the run block, the version and the digest (critical
    labels unchanged: trial `note_pregnancy=yes` 12 of 13, `note_pancreatitis=yes` 6 of 6,
    `note_active_cancer_treatment=yes` 16 of 17, `note_recent_cv_event=yes` 13 of 13,
    `egfr_30_or_more=no` 29 of 29, `no_recent_cv_event=no` 17 of 17; prior authorization
    `red_flag=yes` 25 of 25; referral `urgency=urgent` 113 of 113, `next_step=urgent_review` 124
    of 124; none automated), committed with the three cards and the scoreboard page; the inbox's
    and AI Guard's differ only in the run block and are not. The scoreboard: the trial's 6 and the
    prior-authorization pack's 3 stored results now read "stale: pack changed" (the referral
    router's 3 already did since 23j); `scripts/scoreboard_page.py --check` and
    `scripts/check_scoreboard.py` pass (21 results recomputed from their inputs, no stale ledger
    re-run), and none is run again here. The 23e table (`tests/test_ehr_profile.py`) now pins an
    RTF note (ehr-trial-3, ehr-pa-3 abstain, `unsupported_type`), the site's own payer id, and
    the RxNorm code the EHR adds (read in addition, not missed); every referral patient is
    decided. The sandbox CDS client (`sandbox/cds_client.py`, CI's `sandbox-e2e`) takes only an
    RTF note as unread. Inferno: `conformance.yml`'s `crd-server` job run locally twice
    (2026-11-06, the scripted compose under projects of this session's own, the engine image
    built from this branch under its own tag, the existing `mendari-inferno-scripted:0.15.0`),
    with the same 53 results both times, and `crd_server_v221_expected.json` refreshed from
    them (36 pass, 5 fail, 12 skip; 34, 11 and 8 before): ehr-pa-1's linked note is now read, and
    its order is decided `not_checked` (its payer the site's own payer id), which shows no card and
    declares no coverage information, so 2.3.01, 4.2.3.01, 4.3.3.01 and 4.4.3.01 pass (no card
    is left to carry the `mendari.decision_id` extension), 4.2.3.02 and 4.4.3.02 skip where they
    passed and 4.2.3.03 and 4.4.3.03 skip where they failed (no coverage information to check),
    and 3.4.3.06's reason is rewritten (four patients `not_checked`; only ehr-pa-3, whose note is
    RTF, abstains with `indeterminate` coverage). `sandbox/inferno/README.md` says so. The
    `dtr-payer` job was not run: its golden holds no payer, questionnaire or digest of the pack,
    and its inputs (`scripts/davinci_sample.py`: the payer as the payor's own identifier, codings
    in the pack's systems, a text/plain note) decide as before. Gate G5-6's `a12-ehr-pilot`: the
    23k pending line replaced by its tests; the scorecard's DS-3a to DS-3d are met, graded again
    after the close-out (data shapes at level 3; overall still level 1), DS-4a stays partial with
    one gap of two left, and CW-4a's last column says what Inferno's CRD results are after 23k;
    ADR-0014's status lists 23k. Tests:
    `tests/test_trial_pack.py::test_through_fhir_a_condition_is_read_by_its_icd10_coding_wherever_it_is`
    (in place of `::test_through_fhir_a_condition_is_read_by_its_first_coding_only`),
    `tests/test_trial_pack.py::test_a_medication_request_is_read_with_its_stated_start_and_stop`,
    `tests/test_prior_auth_pack.py::test_the_payer_is_read_by_coverage_reference`,
    `tests/test_referral_pack.py::test_a_referral_under_a_site_code_is_decided_through_the_site_code_map`
    (and `::test_a_referral_under_a_site_code_is_decided_once_the_site_lists_it`, kept),
    `tests/test_ehr_profile.py::test_what_each_pack_cannot_read_yet_is_pinned` (its table),
    `tests/test_ehr_profile.py::test_the_shipped_packs_read_the_profiles_linked_notes_through_the_allow_listed_client`,
    `tests/test_fhir_mapping_03.py::test_a_followed_field_falls_back_to_a_path_of_its_own_type`,
    `tests/test_fhir_mapping_03.py::test_focus_where_reads_a_code_set_and_a_sites_translation_finds_its_focus`.
    Changed deliberately: fixtures whose codings had no system or whose inline note had no content
    type (the trial, prior-authorization, referral, CDS card and unreadable-record tests), the
    trial's patient-view prefetch (its new key), `tests/test_code_systems.py` (the referral focus's
    code set), `tests/test_cds_client_sandbox.py` (an RTF note the only one left unread), `tests/test_spec_03.py` and
    `tests/test_fhir_linked.py` (three reference packs are on 0.3 now),
    `tests/test_fhir_mapping_03.py::test_a_0_2_mapping_written_with_0_3_keys_reads_every_golden_case_alike`
    (the inbox's 0.2 mapping only), `tests/test_sandbox.py::test_referral_cases_round_trip_through_the_pack_mapping`
    (an unlisted document code is not read back), `tests/test_data_coverage.py` (the referral
    focus's path, and `base64_text` counted on the prescreen fixture) and
    `tests/test_ehr_scorecard.py` (rows a step grades again are named together). Behaviour
    changes to know: a CDS client that sends the trial's old prefetch without `medication_requests`
    and no `fhirServer` gets the `412` any left-out key gets; an inline note with no content type
    is unreadable (R4's att-1 requires one); a payor or `medicationReference` the record does not
    hold abstains on `fhir/decide` and `$decide` (they never read a link). Review fixes: notes
    whose joined text is longer than the pack's input schema lets the field be (20,000 characters
    for the trial and prior-authorization packs; a hook reads every clinical note of the patient)
    were refused as invalid input, a BLOCK with no card, so a pregnancy or a red flag in them went
    unseen at the chart; the engine now leaves such a field out of the input, never cutting its
    text, and the decision abstains (`could not read notes: too_large`, `fhir.overlong`, counted
    in the data-coverage record, spec 0.3 §2.4), with its `info` card. The prior-authorization
    mapping's order code lost its first-coding fallback, which `focus_where` made unreachable
    (behaviour unchanged). The cards now state what the review found unstated: the trial's
    criteria count only an `active` request as current, so a request that is not active and has
    no bounds end reads as not taken (a `completed` insulin written 20 days ago reads
    `no_insulin` "yes", an `on-hold` metformin `stable_metformin` "no"), and the first matching
    request decides (the behaviour is the adopted rule's, left for the clinician's review:
    question 3 of the brief now asks it); a `not_checked` prior-authorization order shows no card
    at `order-sign`, a possible red flag in its notes included, which reaches the review queue at
    urgent priority only (left for the owner's payer question below); an order is the
    prior-authorization pack's only by its fictional code through FHIR; a referral checklist code
    is read through FHIR only once the mapping's code set has it too, and a system-less LOINC
    coding is not read as LOINC; and the 20-link cap and the length limit beside RTF in both
    note-reading cards and the threat model's residual risk. The changelogs state the system-less
    codings no longer read (an order's FX code, a referral's FR101, a drug's) and that an unread
    payor reference abstains even beside an identifier. The brief no longer says the trial's
    result is never automated (a governance override may automate one but `candidate`), and its
    review table asks for no name (the gate keeps it outside the repository). Stale lines fixed
    in README, CHANGELOG (the 23m entry's pending lines), ADR-0014, the scorecard's DS-4a, the
    connecting guide, `ci.yml`'s `cds-client` comment and two sandbox docstrings;
    `tests/test_cds_client_sandbox.py::test_only_an_rtf_note_may_be_left_unread` is the renamed
    `::test_only_a_linked_note_may_be_left_unread`. `./scripts/build-reference-pack.sh` rerun:
    data and models byte-identical, the three packs' benchmarks and ledgers differing only in the
    run, the digest and the result's hash, their cards in the known failure modes, the
    scoreboard page unchanged (its results already stale); the FHIR decisions golden unchanged,
    every case; the CRD session not rerun (no decision on the profile's patients changes, nor any
    card's words). Tests:
    `tests/test_trial_pack.py::test_notes_longer_than_the_input_allows_abstain_and_show_a_card`,
    `tests/test_prior_auth_pack.py::test_long_notes_with_a_red_flag_abstain_with_a_card_at_the_order`,
    `tests/test_prior_auth_pack.py::test_a_red_flag_under_a_site_payer_id_goes_to_review_with_no_card`
    (the default, pinned),
    `tests/test_prior_auth_pack.py::test_through_fhir_an_order_and_its_payer_reference_are_read_as_the_changelog_says`,
    `tests/test_referral_pack.py::test_the_mappings_code_sets_hold_exactly_the_checklists_document_and_report_codes`,
    `tests/test_referral_pack.py::test_a_system_less_loinc_document_code_is_not_read_through_fhir`.
  - [x] **23l** (#280) Conformance outside the unit tests: Inferno's CRD kit as the CDS client, the
    SMART App Launch and Bulk Data client suites, the HL7 validator with a terminology server on a
    schedule, and the vendor scope check. **Part 1 built** (Inferno's CRD server, SMART client and
    Bulk client suites, run by a driver against the engine): a second image,
    `sandbox/inferno/scripted/` (CRD kit v0.15.0 by commit, Bulk Data kit v0.14.0, inferno_core
    1.6.0, the lock the build brief resolved), a standalone compose project whose web app and
    worker share one database, `scripts/inferno_session.py` (the REST API driver, the client's
    part in the engine's container), `scripts/inferno_scripted_inputs.py`, the goldens
    `crd_server_v221_expected.json` (groups 1, 2, 3.4, 4: 53 tests, 34 pass, 11 fail, 8 skip),
    `smart_client_stu2_2_expected.json` (public client: 4 of 5 pass, 3.01 is https) and
    `bulk_data_v200_client_expected.json` (8 of 8 pass), each from a local run and confirmed by
    a second; `conformance.yml` jobs `crd-server`, `smart-client` and `bulk-client`. A Bulk export
    deletes itself on the server after the downloads (a failed DELETE is a stable code, never a
    failure), and `mendari sandbox smart-launch --launch-url` takes the launch URL an EHR made.
    Deviations: group 3.7 is not started (3.7.02 names no requirement and cannot pass: the
    must-support elements need data no pack has); the scripted image has no `list_tests.rb` (the
    driver lists a run's tests from its session, with the suite's options and nested groups);
    Inferno's registration is the scripted engine's only CDS client (the root compose's 23e list
    is untouched). Not yet run on GitHub: `conformance.yml` runs weekly on main (its `dtr-payer`
    job), and the first green run of its new jobs there is 23m's record.
    **Part 2 built** (the validator with a terminology server on a schedule, and the scope check):
    `scripts/fhir_validate.py --tx URL` (in place of `-tx n/a`, still the default; refused with
    `--offline`; the run's own package cache from the lock and the "put an unlocked package in its
    cache" check kept, now apart from `-no-http-access`); `.github/workflows/terminology.yml`
    (schedule and dispatch only, `fhir-conformance`'s steps with `--tx https://tx.fhir.org/r4`,
    pinned step for step by
    `tests/test_workflow_safety.py::test_terminology_validation_runs_on_a_schedule_and_never_on_a_pull_request`);
    `mendari smart check` compares the scopes asked with `scopes_supported` (SMART v1 and v2,
    wildcards, the three contexts; `server/scopes.py` `resource_scope`), `fail` for one not
    published, `unknown` for no list (never `ok`, exit 1 on `fail` only), smart-launcher-v2's
    published `patient/*.*` covering the default `patient/*.rs`. Deviations: the `--tx` tests are
    in `tests/test_fhir_packages.py` beside the validator script's other tests and fakes, not a new
    `tests/test_fhir_validate.py`; an empty `scopes_supported` list is `unknown` too. Not run:
    `terminology.yml` itself (never run from a build session; which issues tx.fhir.org raises,
    and so which expected warnings it needs, is known only after its first run, 23m's record).
    **The owner's question**, answered on 2026-11-07 (`fail`, as built): a scope the EHR does
    not publish is `fail` (as 23l built; conflict 56: no published list is `unknown`), though
    SMART 2.2.0 says `scopes_supported` need not be exhaustive; `warn` would match the spec, a
    one-word build PR.
  - [x] **23m** (#280) Close-out. Gate G5-6's `a12-ehr-pilot`: its LP-23 pending probe
    replaced by the 18 tests 23m lists, each found under the name the plan gives it, in groups by
    step (23b and 23c; 23d and 23e; 23f, 23h and 23i; 23l with
    `file: sandbox/inferno/crd_server_v221_expected.json`; 23m's end-to-end test), and three
    pending lines: 23j (after the owner confirms the referral `show` list; confirmed on
    2026-11-06, and the line replaced by 23j's tests in #281), 23k (after the owner's
    go-ahead; given on 2026-11-06, and the line replaced by 23k's tests in #282), and the
    owner's Epic sandbox run in the plan's words (the LP-21n pattern); no
    count in `tests/test_gates.py` changes (conflict 57). The gate stays not met,
    and nothing the owner's is marked met.
    `tests/test_ehr_pilot_e2e.py::test_a_signed_hook_its_card_and_its_feedback_verify_end_to_end`
    (SQLite and Postgres, about a second each, the socket guard of
    `tests/test_scoreboard_end_to_end.py` with its positive control): sign-in on, the EHR
    registered by a pinned `jwks_uri` (its JWK Set an in-process transport, fetched once), ES384
    tokens with `jku`, discovery and two `order-sign` hooks over HTTP for two vendor-shaped
    patients (ehr-ref-2, ehr-ref-4), the record read from the profile's FHIR server with no
    prefetch; one card each (its `uuid` the decision id, `at-most-one`, its suggestion's id
    derived from it); feedback with a signed token, one accepted with its suggestion and one
    overridden with a reason the card offered, chained as `card.feedback` after both decisions
    and reported by `cds epic`; `mendari audit verify` passing, then failing at the changed
    event once the override is turned into an acceptance behind the store's back. Threat model
    residual risks: trust in the EHR's key and its rotation (a retired key still verifies for
    up to an hour on each instance that kept it from a `jwks_uri`, or until a redeploy by value,
    and a rotated-in one is `cds_key` for up to a minute), a compromised `jku` host (a successful
    fetch is not logged; keys by value only for a client whose tokens carry no `jku`, and Epic's
    carry one), the decision id disclosed to the EHR (a chain beside the EHR's records is
    identifiable, after retention too), a misconfigured site map (a wrong translation reaches a
    person on the wrong code; a missing one is not capped), and frequent abstention on RTF-heavy
    records. ADR-0014 Accepted, its status listing what landed with PR numbers (#256, #262,
    #264, #269 and #270 through #271) and the lane's steps, and what is open (23j, 23k, the Epic
    run, the first runs of `conformance.yml` and `terminology.yml`); one more consequence, the
    five residual risks. The scorecard graded again at the close-out commit (`d2ef0201` with
    LP-23m), beside a Before column that is each row as LP-23a merged it (#256), every row on
    evidence in the repository only (a test, a golden, a run recorded there): client trust 2 to
    3, who may call from where 1 to 3, cards and workflow 1 to 2, the other four unchanged
    (failure behaviour 1, data shapes 2, conformance evidence 2, vendor fit 2), overall level 1
    before and after (failure behaviour, FB-2d: the pilot's `MENDARI_FHIR_VALIDATION`, owner
    decision 16). Not counted: WC-4a and CE-3a, which 23e marked met, are partial after, since
    `sandbox-e2e` has not run on GitHub with the signed client yet (#280's run); DS-4a
    waits on `fhir-conformance` the same way, CE-4d on `sandbox-e2e`, CT-4a, FB-4a, CW-4a,
    CE-4a, CE-4b and VF-4a on `conformance.yml`'s first run on main, and CE-4c on
    `terminology.yml`'s. `tests/test_ehr_scorecard.py` checks both columns, that Before is
    LP-23a's (`git show 75ccf3bf`, skipped in a shallow clone), and that no row waiting on a CI
    run that has not happened is met. Docs: `docs/deploying-with-phi.md` §2 (card feedback,
    retiring a key and stopping a client, the decision id the EHR keeps, a site's own codes),
    §8 (`MENDARI_FHIR_VALIDATION`) and §9 (feedback and `audit verify` after a hook, the
    pilot's validation mode, reading the first decisions' data coverage);
    `connecting-an-ehr.md` (retiring a key and the `jku` host, the decision id the EHR keeps,
    "Before a pilot"); `api.md` (a repeated sentence dropped, notes read as text, a site-coded
    decision never automated); the `NOT_A_DATA_SERVER` text (card feedback, an EHR's own token,
    what Mendari reads from an EHR's FHIR server); `deploy/vercel/README.md` (an EHR's own
    tokens on Vercel); the `/docs` landing's CDS Hooks paragraph; README; CHANGELOG. **The first
    green `conformance.yml` run on main** (the acceptance "the CRD client job passes") cannot
    exist before the lane merges: its URL is recorded here after the first run of the new jobs
    on main, and so is `terminology.yml`'s. No pack, figure, format or decision changes.

  **Order:** 23a first; then 23b, 23d and 23g in parallel; 23c after 23b; 23f after 23b and 23d;
  23e after 23b, 23c and 23d; 23h and 23i in parallel, after 23d and 23g; 23j after 23f; 23k after
  23e, 23h and 23i; 23l after 23b and 23d, beside 23e to 23k; 23m last.

  **Conflicts written before building, and the default each PR follows** (in brief; each in full,
  with the code it rests on, in `notes/lp23-plan.md`; each confirmed by the owner on 2026-11-07
  but conflict 39, left for the B21 clinician review, and conflict 41, recorded as built: the
  owner's lines below):
  - 23a (1, 59): the launch plan's EHR grade and its rubric are in no file or commit (A12.goal),
    so rubric v1 is graded at main, the overall grade is the lowest dimension, every grade carries
    its scope and the old grade is not reproduced. Whether Epic's free sandbox can call an
    external CDS service is unverified: the checklist asks.
  - 23b (2 to 14): a JWT-shaped bearer credential up to 8 KiB is recognised on `/cds-services`
    only (`ohk_` and `mat_` unchanged); the CDS branch runs above open mode's shortcut, so a JWT
    is verified in both modes, without a registry it gets `401`, and with sign-in off every
    registered organization is `default` (the public CDS Hooks Sandbox must then be registered
    even for a local demo); `aud` is `MENDARI_PUBLIC_URL` plus the path, exact, never the `Host`
    header; `iss`, keys and FHIR bases are registered apart, with an optional
    `iss_is_fhir_server`; `sub` is checked only when pinned and an unregistered `tenant` claim is
    refused (no Community Connect); `iat` is required, a token lives at most 330 s; the header is
    `alg`, `kid`, `typ` (`JWT`) and `jku` only, Backend Services unchanged; `jku` equals the
    registered `jwks_uri` on the allow-list, keys come through `GuardedHttp`, kept 3,600 s, an
    unknown `kid` refetches at most once a minute per client, a private key caches nothing; the
    registry is `MENDARI_CDS_CLIENTS`, checked at startup; "the same credential" is the same
    client and keys (a `jti` is single-use); kind `cds` holds discover and decide, confined to
    `/cds-services`, outside the notice gate, daily-capped and limited by `cds:<id>`;
    `smart_claim` gains `smart_put`'s bounded purge; a `fhirServer` off the allow-list or not the
    client's is refused (`403` for a `cds` principal before charging or fetching, else `422` as
    today), never abstained on.
  - 23c (15, 16): "local" is a loopback client address, a local `Host` and no forwarding header;
    otherwise only `MENDARI_CDS_SANDBOX=1`, refused on Vercel or with sign-in on, and a verified
    token does not lift it. CORS on `/cds-services` only, for exact registered origins, without
    credentials, the preflight before the guard; CORP unchanged.
  - 23d (17 to 26): neither prefetch nor `fhirServer` is `412`, a failed read from an allowed
    server is `200` with a recorded ABSTAIN; a null prefetch value is no data and is not fetched,
    an OperationOutcome is unreadable; every declared key that could not be read abstains with a
    fixed reason, request faults stay `4xx`, the client's structure errors `400`, and fetched ones
    `not_r4`; `decide(..., unreadable=)` still BLOCKs refused input and abstains before any
    provider; a link-only or undecodable note abstains (`_decode` unchanged, the CHANGELOG says
    so); `data_coverage` (`mendari.data-coverage/1`) holds declared systems, the pack's own codes,
    counts and reason codes only, on every FHIR decision, null on JSON ones, old records still
    verifying; SMART decide shares the per-key read, a failed focus read keeps today's error; an
    unreadable ABSTAIN always shows an `info` card; CRD gets `indeterminate` and `technical` only
    when the Coverage was read.
  - 23e (27, 28): the vendor profile uses fictional site systems, ICD-10-CM, LOINC and RxNorm,
    no SNOMED CT, shaped after public vendor documentation; it stands in for Epic's patients,
    whose run is the owner's acceptance (A12.acc-3), a pending line in the gate.
  - 23f (29 to 36): a card-only `fill_display` with display names, every summary expansion
    checked to fit 140 characters, Task text unchanged; the bare confidence percentage leaves the
    card; the trace goes behind the audit link only with `MENDARI_PUBLIC_URL` set; the launch link
    is a `?pack=` URL only when fully configured; the card's uuid is the decision id; feedback is a
    `decide` rule (same pack and organization, and with sign-in on the same requester; one `404`
    otherwise; all or nothing; the decide bucket, no daily cap; `card.feedback`, codes and ids
    only); `userComment` is dropped; `source.topic` only if Inferno needs it.
  - 23g (37 to 39): spec 0.3 in its own document, 0.1 and 0.2 packs loading and mapping as
    before, 0.3 keys refused in older packs and bounded at load for packs the deployment does not
    own; a MedicationRequest starts at `boundsPeriod.start`, else `authoredOn`, and stops at
    `boundsPeriod.end`, else unknown, never inferred from its status (B21 clinician review still
    owed; answered by the owner on 2026-11-07, acceptable with changes: the owner's lines below).
  - 23h (40, 41): Binary is read as FHIR JSON under the `fhirServer` base only, plain text and
    HTML only, at most 20 reads of 1 MiB per decision, never from `fhir/decide` or `$decide`; the
    payer by `payor[0].reference`, then its Organization's identifier in the pack's payer
    system, else the payor's identifier as written (the plan's last step, the site map, is not
    built: a map translates codings only, #280 and #282; recorded as built).
  - 23i (42 to 44): the site map translates in the adapter (logic files and digests untouched),
    per organization in the environment, its version and sha256 recorded; a `site_code` cap keeps
    those decisions at HUMAN_REVIEW until G5-6 `site-validation`; 23i rebuilds the reference packs
    and expects no diff, 23j and 23k commit their rebuilt artifacts.
  - 23j (45, 46): the inbox pack gets a template and stays off Inferno's list; the referral
    `show` list is `[return_for_info, urgent_review]` (confirmed by the owner on 2026-11-06).
  - 23k (47): built, its merge held for the owner's go-ahead (given on 2026-11-06; the
    clinician's note on the MedicationRequest rule is still owed, B21, answered on 2026-11-07
    with changes to build).
  - 23l (48 to 56): Mendari's own Inferno Dockerfile and overlay, pinned by digest and commit; a
    separate report-only `conformance.yml`; test 1.01 (TLS) and CRD's standard prefetch forms
    are expected results with their reasons; Mendari writes the v2.2.1 session and its golden;
    token refusal is proven by Mendari's own tests; a terminology server on a schedule only; the
    SMART client suite headless if it can be, else by hand; the scope check says `unknown` when
    nothing is published.
  - 23m and requirement 8 (57, 58): the gate's pending probe is replaced by the tests and one
    owner `pending:` line, with no count changing; no refresh tokens or iframe mode are built
    until the sandbox shows the need.
  - Numbers (60): ADR-0014 and threat-model rows S21, T9, R5, I16, I17, I18, D13, D14, E15 and
    E16 are LP-23's.

  **Owner decisions** (defaults first; none blocks 23a to 23i; each default confirmed by the
  owner on 2026-11-07 but (4) and (5), decided on 2026-11-06: the owner's lines below): (1) the
  CDS client registry in `MENDARI_CDS_CLIENTS` for the pilot, a database-backed one before a
  second EHR; (2) the site code map in `MENDARI_SITE_CODES`, site-mapped decisions capped at HUMAN_REVIEW until G5-6
  `site-validation`; (3) approve rubric v1 and its lowest-dimension rule, or supply the original
  scorecard; (4) the referral `show` list, `[return_for_info, urgent_review]`, confirmed 2026-11-06;
  (5) the go-ahead for 23k, given 2026-11-06, with a clinician's note on the MedicationRequest
  rule (B21), still owed (answered 2026-11-07, acceptable with changes, built in the trial pack's
  0.4.0 and awaiting the owner's check); (6) an
  unreadable ABSTAIN always shows an `info` card; (7) the feedback comment dropped, or kept apart
  from the chain, sealed and purged; (8) three neutral override reasons (not relevant, already
  addressed, clinical judgement); (9) attachments: plain text and HTML only, 20 reads of 1 MiB;
  (10) conformance jobs required after four consecutive green weekly runs; (11) the engine trace
  behind Mendari sign-in; (12) the card's launch link as `?pack=`; (13) neither refresh tokens
  nor iframe mode unless Epic needs them (any change amends ADR-0006); (14) the Epic run as an
  owner `pending:` line on `a12-ehr-pilot`, not a seventh G5-6 prerequisite; (15) if Epic's free
  sandbox cannot call an external CDS service, a pilot site's own environment, Oracle Health's
  sandbox as the backup, and test keys and Inferno until then; (16, raised by the 23a scorecard,
  not in the plan) the pilot's `MENDARI_FHIR_VALIDATION`: `warn`, as today, under which input
  that is R4 JSON but breaks R4's other rules is decided with its issues recorded and failure
  behaviour stays at level 1 (FB-2d), or `enforce`, which blocks it before any provider runs.

  **The owner's actions left**, one line each (the Epic ones in the order of
  `notes/epic-sandbox-checklist.md`). **2026-11-07:** the owner wrote, in chat, "the patent gate
  is clear and I approve everything on my part". That confirms each default this entry states:
  owner decisions 1 to 3 and 6 to 16; conflicts 1 to 60 but 39, with 41 recorded as built (no
  site-map step for the payer); the lane's three questions as built; and the pull requests'
  questions listed below. It leaves open conflict 39 (the MedicationRequest rule, B21) and two
  clinical questions for the owner as a clinician: whether a `not_checked` order shows its card
  (#282 Q2) and whether a referral with no card should show one (#281 Q3, beside decision 4).
  Later on 2026-11-07 the owner, a physician, answered all three by picking options (the lines
  below); each change is a build PR next. The owner's own actions below stay the owner's:
  - [ ] Owner (decision 15): sign up for Epic's sandbox from `notes/epic-sandbox-checklist.md`,
    and note back its `iss`, `jku`, `aud` form, `sub` and `tenant`, feedback support, scopes and
    launch handling, and whether it can call an external CDS service at all.
  - [ ] Owner: put Epic's JWKS origin and FHIR base on `MENDARI_FHIR_ALLOWED_SERVERS`.
  - [ ] Owner: run `mendari smart check` against Epic's FHIR base (the scorecard's CE-5a).
  - [ ] Owner: do the Epic sandbox run (every pack with a standard hook decides on Epic's
    sandbox patients, A12.acc-3), which lets a build PR remove the gate's pending line.
    - 2026-11-08: first SMART launch on Epic's sandbox (owner): launch, authorize, callback,
      patient read worked; the trial pack abstained for Anna Cadence on three gaps
      (MedicationStatement not offered, the shared 20-link cap, RTF twins counted twice), built
      in #290 (merged 2026-11-09); the owner's run stays open; the trial card's "20 links"
      wording waits on the trial pack's next version (O4 below). Run notes:
      `notes/epic-sandbox-checklist.md` section 7.
    - 2026-11-10: the same launch after #290 (merged 2026-11-09) and #287, with the owner's
      MedicationStatement declaration on the sandbox only, decided for Epic's test patient:
      `HUMAN_REVIEW`, `not_eligible` on `stable_metformin` "no" from its default, the
      limitation stated; nothing unread (3 notes, 2 orders, both drugs read), so only the
      declaration was exercised, not #290's budgets. Epic's certificate chain (2026-11-09) and
      its test tool's DSTU2 `iss` (early afternoon) failed first, both on Epic's side. The run
      stays open: the other packs and the hook path. Checklist section 7.
    - [ ] Owner (O1): set `MENDARI_EHR_DECLARATIONS` for Epic's base on the hosted site, a
      production setting and a clinical claim, only after the checks in
      `docs/guides/connecting-an-ehr.md#what-your-ehr-does-not-offer` (where Epic keeps reported
      medications and with which `intent`, given #287's `listed`; whether its default search
      returns stopped and inpatient orders; whether drug names carry the generic name). The code
      ships it off.
    - [ ] Owner (O2): RTF-only notes, A (today: abstain, now with the format counted), B (decide
      on the readable notes, at most HUMAN_REVIEW, the unread notes stated; a spec 0.3 change
      and a pack opt-in) or C (a bounded RTF-to-text reader with its own tests). No
      recommendation. The 2026-11-10 run read all 3 of its notes (none RTF only, so no
      `unread_formats`), too few to say how often; a chart with more notes says it.
    - [ ] Owner (O3): name-first's safe form (a follow read only when no earlier path of the
      field gives a value; one sentence in spec 0.3 §2.3), only with a pack change that orders a
      MedicationRequest's own paths first.
    - [ ] Owner (O5): under a declaration, whether a list-based answer that came from its
      criteria's `default` (`no_insulin` "yes" when no insulin is listed) reads `unknown`
      instead. Not built: today it reads "yes", recorded with the limitation and never
      automated. Seen on Epic on 2026-11-10: `stable_metformin` "no" from its default (neither
      of 2 stopped orders names metformin) made the prescreen `not_eligible`, and `no_insulin`
      read "yes" by its default (checklist section 7).
    - [ ] Owner (O6): under a declaration, whether the trial card shows for every decision
      (its `show` hides `not_eligible`, which an incomplete list can give); `shows_card` (#288,
      merged) could treat a declared limitation as it treats an unreadable record. The review
      queue holds every such decision today.
    - [ ] At the trial pack's next version (O4; #287 merged it at 0.4.0 without these): the
      trial card's "more links than one decision reads (20, `cap`)" wording (now 50 notes and 50
      followed references), a date window or `_count` on the notes prefetch, and whether to add
      `_include=MedicationRequest:medication` (unverified on Epic).
    - [ ] Next Epic run: why the clinical-note search found 3 notes on 2026-11-10 and 21 on
      2026-11-08 with the same prefetch (Epic's sandbox data, its paging, or another patient);
      and a chart with more than 20 notes, to show #290's budgets and per-note counting on Epic.
    - [ ] At prior-auth's next version (#288 merged without it): the pack's "20, `cap`" wording
      in `pack.yaml`, `DECISION_CARD.md` and its CHANGELOG (now 50 notes and 50 followed
      references).
    - [ ] Dashboard: the criteria table in a derived primary's audit drawer (`pack.js`
      `criteriaTable`) is a scrollable region with nothing focusable in it (axe
      `scrollable-region-focusable`, seen on a trial record's drawer, 2026-11-08); fix on its
      own.
    - [ ] Follow-up: an HTML+RTF twin patient in `sandbox/ehr_profile.py` (changes
      `test_what_each_pack_cannot_read_yet_is_pinned` and the CI validator's sample); and the
      prefetch reads under a clock (threat model D14's residual).
  - [x] Owner (decision 4): confirm the referral `show` list, so 23j merges: confirmed
    2026-11-06 ("Confirm the default": `[return_for_info, urgent_review]`), built in #281.
    What the list leaves without a card, for the owner to see: a referral Mendari refuses as
    invalid input (BLOCK) shows none and is not in the review queue, so only the EHR's own
    referral workflow carries it; and a referral to schedule, which `rules_and_jev` may automate,
    shows none, so no one signing it sees the suggested specialty (checking it against the
    referrer's is out of scope, future work). Say if either should show its card. Answered by
    the owner as a clinician on 2026-11-07 (#281 Q3): (a) a refused (BLOCK) referral shows a
    card; (b) a referral to schedule stays without one. (a) is built: the Referral Router 0.3.1's
    `refused` card (spec 0.3 §5), no decision changed (the rebuild, the card census and the CRD
    golden checked).
  - [x] Owner (decision 5): the go-ahead for 23k: given 2026-11-06 ("go ahead do it, full consent"), built in #282.
  - [ ] Owner (B21): a clinician's note on the MedicationRequest rule; the draft for the reviewer is notes/clinician-review-medication-period.md, and gate clinician-review's attestation records the review. Its question 3 includes what the 23k review found: a listed insulin or metformin that is not `active` and has no bounds end reads as not taken (a `completed` insulin written 20 days ago reads `no_insulin` "yes", not "unknown"); the card states it, and the behaviour waits on the answer.
    **Answered by the owner, a physician (also the pack's recorded author, disclosed), on
    2026-11-07** (the brief's table): Q1 the start as built; Q2 an `active` request with no end
    is current only if written or started in the last 12 months, and an older one reads unknown
    and goes to review; Q3 a request that is not `active` and has no end reads as not taken, as
    built, no stop taken from its status; Q4 renewals kept separate, as built; Q5 intent
    `proposal`, `plan` and `option` and `doNotPerform` true left out (`order`, `original-order`,
    `reflex-order`, `filler-order` and `instance-order` kept); Q6 `boundsDuration` read too
    (start plus duration is the stop), the first dosage line only; Q7 the same 12-month rule for
    an `active` MedicationStatement with no end, nothing else changed; Q8 acceptable with changes
    (Q2, Q5, Q6, Q7). **Built** on `claude/b21-medication-rule-5b433a` (the Clinical Trial
    Prescreener 0.4.0, spec 0.3's new criteria key `current_within_days`), not merged until the
    owner checks its worked examples; it also asks whether an `active` medication whose end has
    already passed should still count as current, as it does now. The build PR has those
    changes (spec 0.3 §2.7, `standards/fhir.py`,
    the trial pack's criteria, a MINOR bump, the rebuilt reference pack and the FHIR golden, and
    the card saying the rule's review was answered with changes and awaits the owner's
    confirmation); then the owner sees and confirms the rule as built, its digest goes in gate
    `clinician-review`'s attestation, the owner's, and only then does the card name the reviewer
    by role. Until then the gate stays unattested and the rule is not clinically reviewed.
  - [x] Owner's rule for reviewers (2026-11-07): the owner's disclosed physician review counts for
    the five reference packs, the owner's own demonstrations ("Remember these packs would be the
    first few that I myself built to demonstrate how it is done. Other packs will be done by
    developers"); a pack from anyone else keeps "a clinician other than the author". Recorded in
    CONTRIBUTING.md and the authoring guide (GOVERNANCE.md and the pack PR template already
    agree); gate `clinician-review` is unchanged.
  - [x] Next (the owner asked, 2026-11-07): review briefs like the medication brief, for the
    trial pack's criteria as a whole, the inbox router, AI Guard's dose table, and the referral
    router with the prior-authorization preflight: built in #283 (`notes/clinician-review-*.md`,
    indexed in `notes/README.md`).
    - [ ] Follow-up, before the referral and prior-authorization review is recorded: refresh
      `notes/clinician-review-referral-and-prior-auth.md`, which still describes both packs at
      0.3.0, for both at 0.3.1 (#288, merged before #283; referral-router `sha256:b283d671…`,
      prior-auth-preflight `sha256:e2b2503f…`): the version and digest, the quoted line numbers,
      the referral router's `refused` card and prior authorization's `also_show` red-flag card on
      `not_checked`.
    - [ ] Owner: review each brief (the trial criteria, the inbox router, AI Guard's doses, the
      referral router and prior authorization) and record each review in gate
      `clinician-review`'s attestation; a build session never records one.
  - [x] Owner (decision 3): approve rubric v1 and its lowest-dimension rule, or supply the
    original scorecard: approved (owner, 2026-11-07); `notes/ehr-scorecard.md` says so.
  - [x] Owner (decision 16): choose the pilot's `MENDARI_FHIR_VALIDATION`, `warn` or `enforce`
    (the scorecard's FB-2d): `warn`, as today (owner, 2026-11-07), so FB-2d stays partial and
    the overall grade level 1; nothing is set in Production until a pilot is signed.
  - [ ] Owner (decisions 1 and 2): once a pilot is signed, set `MENDARI_CDS_CLIENTS` (and any
    `MENDARI_SITE_CODES`) for Production only.
  - [ ] Owner (decision 10): after four consecutive green weekly `conformance.yml` runs on main
    that include its new jobs (`crd-server`, `smart-client`, `bulk-client`), make its jobs
    required (a ruleset change). Its three green `dtr-payer`-only runs on main so far
    (2026-10-20, 2026-10-27, 2026-11-03) do not count toward the four. The rule is confirmed
    (owner, 2026-11-07): 0 of 4 so far, the earliest fourth on Tue 2026-12-01.
  - [x] Owner: answer the lane's three questions: a request-wide read and byte budget for
    linked reads (23h), how a payer field reads a site's payer identifier (23i, for 23k; 23k
    built without it: the prior-authorization pack reads the payer's Organization by the pack's
    own payer system or the payor's identifier as written, so a site's own payer id reads as
    `other` and the order as `not_checked`, which shows no card at `order-sign`, so on a real
    EHR's data a possible red flag in an order's notes reaches only the review queue, at urgent
    priority, and not the clinician signing it; the card states this, and the answer may also
    say whether `not_checked` should show a card, or the red-flag rule set a recommendation that
    does), and whether a scope the EHR does not publish is `fail` or `warn` (23l). Answered as
    built (owner, 2026-11-07): (1) no request-wide budget for now, the bound stays a decision's
    (residual D14); (2) the pack's payer system, else the payor's identifier as written (= #280
    Q8b, #282 Q1; a code set reading identifiers would be engine work); (3) `fail` (as 23l
    built; conflict 56: no published list is `unknown`).
    For the owner as a clinician (#282 Q2): whether a `not_checked` order shows its card, or the
    red-flag rule sets a recommendation that does. Answered on 2026-11-07: a possible red flag
    under a site payer id shows a card at order signing; other `not_checked` orders stay quiet.
    Built: the Prior Authorization Preflight 0.3.1's red-flag card (`cds_hooks.card.also_show`,
    spec 0.3 §5), no decision changed (the rebuild, the benchmarks, the FHIR golden and the CRD
    golden checked); the test is now
    `tests/test_prior_auth_pack.py::test_a_red_flag_under_a_site_payer_id_shows_a_card_at_signing`.
  - [x] Owner (#280 Q2): the card's `mendari.decision_id` extension, which CRD 2.2.1's response
    model does not define, stays a recorded gap (3.4.3.01 and 4.1.3.01 expected failures);
    moving the id would change the card format. Confirmed (owner, 2026-11-07).
  - [x] Owner (#280 Q3): CRD group 3.7 stays unstarted (3.7.02 names no requirement), no golden
    field added. Confirmed (owner, 2026-11-07).
  - [x] Owner (#280 Q4): the Bulk client deletes a finished export by default, real EHRs
    included: one try, a failure a stable code that is not fatal. Confirmed (owner, 2026-11-07).
  - [x] Owner (#280 Q5): the scripted compose's `MENDARI_CDS_CLIENTS` keeps Inferno's CRD client
    only, not 23e's sandbox client. Confirmed (owner, 2026-11-07).
  - [x] Owner (#280 Q6): a spec 0.2 pack reads an inline RTF note with its markup, and does not
    abstain; moot for the shipped packs (the two on 0.2, the inbox and AI Guard, read no
    attachment). Confirmed (owner, 2026-11-07).
  - [x] Owner (#280 Q7): a later PR marks WC-4a, CE-3a and the `conformance.yml` and
    `terminology.yml` rows met once their runs on main are green (the regrade, a build item);
    until then a row waiting on a run reads `partial`, not met. Confirmed (owner, 2026-11-07).
  - [x] Owner (#280 Q8c): a hook whose `fhirServer` is off the allow-list is refused, nothing
    fetched or recorded: `403` for a CDS client's own token (before anything is charged), else
    `422` (conflict 14, as built and tested). #280's body labels the recorded ABSTAIN with `200`
    as the built behaviour; it is not. Confirmed (owner, 2026-11-07).
  - [x] Owner (#280 Q8d): a rule's BLOCK reached through a site translation goes to
    HUMAN_REVIEW (policy only lowers automation). Confirmed (owner, 2026-11-07).
  - [x] Owner (#281 Q1): a card-only change is a PATCH (the inbox's 0.4.3, the referral router's
    0.2.1, since 0.3.0 by #282). Confirmed (owner, 2026-11-07).
  - [x] Owner (#281 Q2): the inbox's card levels: `info` for the front desk, a medical assistant
    and billing, `warning` for a nurse, a physician and no team, a possible emergency `critical`
    (stat); every message gets a card. Other levels would be a PATCH card change. Confirmed
    (owner, 2026-11-07).
  - [x] Owner (#281 Q4): the stale scoreboard results are run again in a later PR, local
    configurations only: 18 stale today (inbox 6, trial 6, prior authorization 3, referral 3; AI
    Guard's 3 current), 15 of which `./scripts/build-scoreboard.sh` refreshes; the trial's 3
    `structured=cql` results wait on the `cql` statement. Confirmed (owner, 2026-11-07).
  - [x] Owner (#282 Q1): how a payer field reads a site's payer id, the lane's question 2 above:
    as built. Confirmed (owner, 2026-11-07).
  - [x] Owner (#282 Q3): `focus_where {path, code_set}` and the own-type fallback after a
    `follow`, in this form (spec 0.3 is a draft), not a per-path follow. Confirmed (owner,
    2026-11-07).
  - [x] Owner (#282 Q4): the first-coding fallback for conditions, labs, diagnoses and
    procedures, after the pack's own system (a strict read turned a site-coded record into a
    BLOCK with no card). Confirmed (owner, 2026-11-07).
  - [x] Owner (#282 Q5): the prior-authorization order's `focus_where` takes no code set, so the
    pack's fictional FX codes only; a site's own CPT order codes through the site map would be
    build work. Confirmed (owner, 2026-11-07).
  - [x] Owner (#282 Q6): a CDS client that sends the trial's old prefetch, without
    `medication_requests`, and no `fhirServer` gets `412` (CDS Hooks 2.0; discovery lists the
    key). Confirmed (owner, 2026-11-07).
  - [x] Owner (#282 Q7): a site that adds a checklist code to `logic/checklist.yaml` adds it to
    the mapping's code set too, or translates it in `MENDARI_SITE_CODES` (capped); a test keeps
    the shipped lists equal. Confirmed (owner, 2026-11-07).

**Container images wait until they comply (owner, 2026-10-07):** after a licensing analysis the
owner received in October 2026, no image goes to GHCR until it carries what the licences of
everything in it ask, and `release.yml` refuses to push one that fails the checks. Images are
pushed only by `release.yml`'s `publish-images`, on a release to PyPI (the TestPyPI rehearsal
pushes none), so the first published images wait on the item below; the PyPI release does not.

- [ ] **Image licence compliance** (#180, draft, branch `claude/image-licence-compliance`; the
  images wait on it). Every image carries `/licenses`: Mendari's `LICENSE` and `NOTICE`,
  `SOURCE.md` (where the corresponding source is), `THIRD_PARTY_NOTICES.md` and `inventory.json`
  written by the build from what the image holds (`scripts/image_notices.py`, run inside the
  image on its own Python), `third-party.cdx.json` (what an SBOM scanner cannot see by itself)
  and `third-party/` (texts). `licenses/image/` keeps what the image would otherwise lack:
  `components.toml` (41 components, each with its version, licence, source, and texts fetched
  from its upstream at that version: what psycopg-binary 3.3.6 bundles on each architecture, from
  its wheels' auditwheel SBOMs and its build (libpq 18.6, OpenSSL 3.5.8, OpenLDAP 2.6.14; on
  amd64 CentOS 7's Cyrus SASL, MIT Kerberos, com_err, keyutils, libselinux and PCRE and
  manylinux2014's libxcrypt 4.5.2; on arm64 AlmaLinux 8's, with PCRE2, libxcrypt 4.1.1 and
  OpenSSL 1.1.1k), what cryptography, cffi, PyYAML and google-re2 link statically, the Rust
  standard library, CPython 3.12.15's incorporated software and pip 25.0.1's 17 vendored
  packages), `crates.toml` (the 143 crates the Rust packages' own SBOMs list, 75 distinct texts
  from crates.io, each crate checked against its SBOM's SHA-256), 130 texts with `SHA256SUMS`
  (`scripts/image_licence_texts.py`). GPL and LGPL components name their exact source pinned by
  SHA-256 (keyutils' and libxcrypt's SRPMs, libxcrypt 4.5.2 with manylinux's build scripts,
  psycopg's tag and sdists, certifi, pip, CPython). Debian's `/usr/share/doc/*/copyright` (the
  base image's slim configuration keeps them; nothing removes them) and
  `/usr/share/common-licenses` are checked present. The label keeps `licenses=Apache-2.0` with a
  description naming it Mendari's only and pointing at `/licenses`.
  `scripts/check_image_licences.py` fails an image with anything uncovered (a package without
  its copyright file, a bundled library, crate or native package at a version nobody reviewed, a
  text missing or changed, stale notices, the wrong LICENSE or NOTICE, the label), and scans an
  SBOM: permissive, or copyleft with its source published, or a reviewed exception (GCC's
  runtime, libcrypt1 and debian-archive-keyring by their copyright text's hash, xz's notice on
  the GNU licence documents, distlib's launchers in pip); unknown fails. `release.yml`: BuildKit
  attaches an SBOM of each finished image (`docker/buildkit-syft-scanner` 1.12.0 by digest), each
  architecture of each image is checked and its SBOM scanned, the run keeps the SBOMs with each
  package's source location (90 days), a new `image-sources` job collects every Debian source
  package at its installed version (deb.debian.org, then the security archive, then
  snapshot.debian.org; each file checked against its `.dsc`) and the components' sources into a
  files-only image, and `publish-images` (now after it) pushes it as `<tag>-sources` before
  each image. CI's `docker` job runs the image check. Measured on the engine image built locally
  (arm64 native, amd64 under QEMU): both pass, the SBOMs' 330 and 327 packages allowed; the
  sources are 61 Debian source packages and 13 other files, 345 MB (one 339 MB layer), collected
  in 12 s. Size (compressed, on disk): arm64 71.8 and 335 MB before, 72.0 and 338 MB after;
  amd64 69.6 and 303 MB before, 69.8 and 307 MB after (`/licenses` is 3.3 MB).
  **Not done, by choice (confirmed below):** attaching the sources to a GitHub release (no
  release is created today, and it needs `contents: write` on a publishing job, which the
  workflow safety rules do not allow; the `-sources` tag in the same GHCR repository is the
  source's place instead). **Not done, for the owner:** counsel to confirm the approach
  (equivalent access from the same place, no separate written offer). Keep each `-sources` tag
  as long as its image, and make it public with the image's package. **Confirmed by the owner
  on 2026-11-07** (in chat: "the patent gate is clear and I approve everything on my part"):
  `SOURCE.md`'s fallback ("open an issue in the Mendari repository and the maintainers will send
  it"), which commits the maintainers, with no other contact named; and the sources' place,
  `<tag>-sources` in the same GHCR repository, kept as long as its image, not a release
  attachment.
  **Review (2026-10-08), six findings, all fixed:** `THIRD_PARTY_NOTICES.md` now gives a source
  for every Python package (PyPI at its version, the component's for psycopg, certifi and pip,
  the repository for Mendari) and every crate (crates.io), and Debian's section says where each
  source package is (`inventory.json` gives each one's snapshot.debian.org address); the SBOM's
  source locations are now read from that inventory, so the two always agree, and a package the
  SBOM lists that the inventory does not fails the release (`no source location`). The SBOM with
  source locations is the one in `<tag>-sources` (`/sources/sbom/`): README and SOURCE.md say
  so, and that BuildKit's attestation has none (pushing the enriched copy as an OCI referrer
  would add a tool to the publishing job that nothing can exercise before a release). CI's new
  `image-sbom` job runs the release's SBOM scan on every change, with the release's generator
  and builder (a test keeps them the same), so a base image bump that renames a Debian licence
  or changes a reviewed copyright text fails in its own pull request. pip's vendored Requests
  carries Requests' NOTICE file (Apache-2.0 4(d); none of the other Apache-2.0 components has
  one upstream). CPython's corresponding source adds the official image's Dockerfile that
  compiled it (docker-library/python 2a3b794c, 3.12/slim-trixie, pinned by SHA-256; its readline
  and _gdbm modules link GPL-3.0 libraries), and `base_image` ties that recipe to the
  `Dockerfile`'s base, so a new base image is reviewed again. The `Dockerfile` writes the
  notices in a stage of its own instead of `RUN --mount`, so `docker build .` works again with
  Docker's classic builder (a test keeps BuildKit-only syntax out). Not done: CI does not
  collect the sources (`image_sources.py`): every run would fetch about 345 MB from Debian's
  archive, and snapshot.debian.org as pinned versions leave the pool; no scheduled job, the
  default (confirmed, owner, 2026-11-07).

- [x] **LP-10** Statistics and a per-case ledger behind every number. Split in three PRs:
  - [x] **LP-10a** The statistics, the ledger, the report format and `mendari compare`
    (#153). `mendari.evals.stats`, pure Python: Clopper-Pearson from the beta quantile (a
    Stirling-form incomplete beta, Halley inversion), exact McNemar, a seeded percentile
    bootstrap and its paired form, Holm over a named family, an equal-mass ECE; checked against
    scipy and statsmodels (development dependencies only) to 1e-12, the bootstrap on the same
    index matrix. A run keeps a ledger (`mendari.ledger/1`: per case a keyed hash of its id,
    truth, prediction, probabilities, outcome, critical positives, cost; no text, no id) and its
    report (`mendari.eval/2`) computes its result only from it, with a `run` block apart; a v1
    report still loads (card, leaderboard, sharing). The five reference packs are rebuilt with
    their ledgers; CI checks each result byte for byte and against its ledger. A stored report
    is chained (`eval.recorded`). `mendari compare A B --dataset X`: paired, exact McNemar,
    Holm, a bootstrap on Brier, the changed cases. Spec: `docs/spec/evaluation-reports.md`.
  - [x] **LP-10b** Every proportion shown as k of n with its interval (card, CLI, dashboard),
    the 100-positive evidence rule, leaderboard rows keyed by pack digest and dataset hash with
    "earlier version" rows unranked, and one ranking function (critical cases first). Decisions
    taken (recommended): nothing ranks without 100 positives on a critical label; the 166
    committed rows, all from earlier versions and 144 of them remote, are kept and marked, not
    re-measured (paid calls). Split in two:
    - [x] **10b-i** (#154) The card, the command line, the monitoring and gate messages,
      `mendari.leaderboard/2` and `mendari.rank/1` (`mendari.evals.ranking`). The trial and
      prior-auth packs' known failure modes no longer type in percentages (0.1.5, 0.2.1:
      wording only).
    - [x] **10b-ii** The dashboard and the Hub show every share of cases as k of n. Design
      chosen by a judged workflow (server figures; three designs): Python works out each
      figure once and the browser only formats it.
      - [x] **A** (#155) `mendari.evals.figures` (`mendari.figures/1`) served beside
        reports, leaderboards, the overview's periods, monitoring and registry versions;
        `render.shown` prints from k and n (never a stored bound); the ranking recomputes its
        bounds; `tests/fixtures/shown.json` for the ports; the equal-mass ECE on the card.
      - [x] **B** (#156, merged 2026-10-07) The dashboard: a `shown` port in `ui.js` checked
        against the fixture (all 3,003 cases); the report, leaderboard (`row.rank`,
        `standing_text`, `groups`), monitoring and overview read `figures`; the subgroup
        false-automation column goes (the card has none); a bare-percentage scan over five
        views, shown to catch one.
      - [x] **C** (#157) The Hub: `hub/lib/figures.ts` (checked against the fixture), the
        pack page's leaderboard in served order (it ranked by accuracy alone), the benchmark
        fact and the search cards as k of n. Its card was raw Markdown tables (no GFM) until
        LP-18a (#228) rendered them with `remark-gfm`, and took the benchmark fact and the
        leaderboard away (ADR-0012).
      - Owner, optional: give the 166 shipped `mendari.leaderboard/1` rows their accuracy
        counts from the committed datasets where provable (the dataset unchanged since the
        row's run, k/n exactly the stored rate). Without it they show accuracy as n/a. Not
        done, the default, confirmed by the owner on 2026-11-07 (in chat: "the patent gate is
        clear and I approve everything on my part").
    - [ ] Follow-up: the five packs' known failure modes still quote counts from hand-written
      sets (e.g. "8/12 in the held-out set"), measured on earlier pack versions. They agree
      with the card's provider comparison today; replacing them with the command that measures
      each (as 10b-i did for the local-site percentages) is a wording change per pack.
  - [x] **LP-10c** The model that answered, on each answer and in each report; and removing
    `DEFAULT_MODEL` (an `llm` variant with no model fails as `not_configured`, in `preflight`).
    - [x] **10c-i** (#158) The model that answered: Jev already recorded it; the `llm`
      provider now stamps the id the endpoint reports (well formed only, else the configured
      one, and the call stands) on each answer, and `vote` names each member that answered with
      its model. No format bump: the spec already defined `models` as the models that answered;
      earlier `llm`/`vote` runs named the configured one (CHANGELOG).
    - [x] **10c-ii** (#171) No default model: an `llm` variant asks the model it pins, else
      `LLM_MODEL` (empty is unset), else it is `not_configured`, in `preflight` and wherever a
      request is built, so no request carries no model and nothing is charged. A decision that
      needs it goes to a person, never AUTO (HUMAN_REVIEW, or ABSTAIN when the variant answers
      the primary question); inside `rules_and_llm` the rules still answer. `mendari
      calibrate` refuses in one line before anything else; `mendari agreement` refuses when a
      variant, or a composite's member, cannot be called. Health: `llm.default_model` is null
      when unset. Inbox pack 0.4.2 (a manifest comment named the default; digest only).
      Breaking for the command line and self-hosted servers (CHANGELOG). Owner check done
      2026-10-07; the PR's Preview health is checked after CI, before merging. Review: the
      README, the authoring guide and the deploy steps say an `llm` variant needs `LLM_MODEL`.

- [x] **LP-11** Evaluation-set store and blind labelling (ADR-0011, Proposed; built 2026-10-17; gates `labelling-underway` and `hidden-sets-frozen` probed by tests, owner attestations open).
  **Owner decision (2026-10-07), where hidden sets live:** set content, labels, assignments and the
  hidden-case index live only in the hosted engine's own database (locally, a developer's SQLite or
  Postgres with synthetic sets), in tables prefixed `evalset_`; never in this repository, a pack
  directory, a shipped fixture or a pack digest. Every case input, label and attribute is sealed
  (AES-256-GCM, bound to kind, set id and case id) under `MENDARI_EVALSET_KEY`, a ring with
  `MENDARI_EVALSET_READ_KEYS` rotated as in LP-09 (its own rotate command and read-only check).
  `MENDARI_EVALSET_INDEX_KEY` keys only the LP-20 hidden-case index and the chained case-id hashes;
  label values are chained as salted commitments (as notes are), never under the index key, so that
  key alone recovers no label. Both keys differ from each other and from every PHI key, are set by
  the owner as Secret for the hosted production environment only (preview database copies then hold
  only ciphertext), and the feature stays off without them. The freeze commitment is a salted hash
  chained at freeze; a signature, if any, waits for LP-17's grading-key decision. `audit verify`
  checks sealed rows against their chained hashes. The backup is a sealed export (ciphertext only).
  Roles: the reserved `labeller` and `grading` (`core/accounts.py`). The key holder can decrypt
  offline, so "only the grading role reads active sets" is a rule the software enforces. CI and local
  runs use synthetic sets and throwaway keys. Owner, later: before any real set, generate the two
  keys, keep a copy off the laptop and set them; before contracting labellers, decide how they reach
  the hosted site while Vercel Authentication is on. Hand-written cases only until B20.

  Planned by a mapping workflow (2026-10-09) as fifteen PRs, each safe alone, the feature off
  without both keys; the full plan, its 57 conflicts with the default each PR follows, and the
  risks are in `notes/lp11-plan.md`. Each conflict's default was confirmed by the owner on
  2026-11-07 (the owner's words under the owner decisions below), but what owner decisions 2
  and 3 leave open: conflict 47's values (decision 2) and, in conflict 40, whether adjudicators
  see both labels and what settles a three-way split (decision 3); the owner decided those later
  that day (below). ADR-0011 stays Proposed (decision 7). Guards come first: keys, permissions
  and a generated no-leak sweep land before any evaluation-set route exists.
  - [x] **11a** (#220, merged 2026-10-09) One canonical form of a case's input (`evals/canonical.py`: the strict
    form and `norm/v1`, pinned byte for byte) and the coverage report (`mendari.coverage/1`,
    `mendari data coverage PACK --dataset X`, over a pack's dataset or any JSONL file: distinct
    cases and positives, the warning under 100, disagreeing duplicates, the language, source and
    stratum mix, never a case's text); ADR-0011; this plan. AI Guard's test set: 164
    `verdict=block`, 30 distinct (recorded; `mendari.eval/2` unchanged).
  - [x] **11b** (#221, merged 2026-10-09) Rater agreement (`evals/rater_agreement.py`, `mendari.agreement/1`,
    pure): percent agreement as k of n with Clopper-Pearson, Cohen's kappa per rater pair and
    Krippendorff's nominal alpha, exact, each with a seeded percentile bootstrap
    (`stats.bootstrap_defined` counts the resamples it drops); derived questions through the
    pack's table over each rater's member labels. Checked against Krippendorff (2011) exactly,
    statsmodels and scipy on the same index matrix.
  - [x] **11c** (#222, merged 2026-10-09) Evaluation-set keys and sealing, nothing stored yet
    (`core/evalset_keys.py`: `MENDARI_EVALSET_KEY`, `MENDARI_EVALSET_READ_KEYS`,
    `MENDARI_EVALSET_INDEX_KEY`; off unless both are set, malformed, half-set or reused keys
    stop the server, which then needs sign-in and, on Vercel, Production). Every token names its
    key and is bound to its kind, set, case and label; case and index hashes are keyed by the
    index key. The PHI policy's sealing moved into `phi.seal_with` and `phi.open_with` with its
    bytes unchanged (tokens sealed before still open). Health shows the key ids to readers.
  - [x] **11d** (#223, merged 2026-10-09; owner's go-ahead 2026-10-09) Permissions, no route needs them yet:
    `label` (the `labeller` role, memberships mode only; a label-only member sees a page saying
    so), `curate` (people; administrators, who hold neither `label` nor `grade`), and `grade`,
    held only by a grading key the operator mints (`mendari access grader-key`, at most 365
    days, chained; listed and revoked, never rotated; refused over HTTP); open mode holds none
    of the three. ADR-0004 and ADR-0008 amended; threat model E2 and E11.
  - [x] **11e** (#224, merged 2026-10-09) Store I: sealed sets and cases, the hidden-case index,
    `audit verify --evalsets` (the operator's command line only): `core/evalsets.py`,
    `mendari evalset create|add|remove-case|list`, hand-written cases only (public cases refused
    exactly and under `norm/v1`), each case chained as its keyed hash and a salted commitment
    (`case_ref`), stray, retired and PHI keys refused before every write and at startup; the
    runbook `docs/operations/evalsets.md`. The sealed freeze record's column is `freeze_record`
    (FREEZE is reserved in Postgres).
  - [x] **11f** (#225, merged 2026-10-09) Key lifecycle: rotation with its check, reindexing, the sealed export:
    `mendari evalset rotate --to <id> [--batch N] [--check]` (copied from `phi rotate`: chained
    `evalset.rotated` batches, compare-and-set, retired keys seal nothing, every set state; the
    check reads only), `mendari evalset reindex` (the whole index under the current index key,
    one transaction, `evalset_index.rebuilt`, earlier index keys retired), `mendari evalset
    export` (the rows as stored: ciphertext, ids and key ids, no key, nothing chained; 11g allows
    its case ids and sealed tokens, conflict 56); `SEALED_COLUMNS` and its pin; the runbook's
    "Rotating the evaluation-set key".
  - [x] **11g** (#227, merged 2026-10-09) The generated no-leak sweep, before any evaluation-set route (tests
    only): `tests/test_evalset_leaks.py` plants a mark in every field of two organizations' draft
    sets and calls every mounted route and method as every kind of principal, scanning answers,
    headers, NDJSON, logs, every `mendari evalset` command, the audit and feedback exports, the
    sealed export file and every table (a mark only where conflicts 38, 55 and 56 allow it);
    each evaluation-set event's payload keys pinned; the route inventories (`test_auth`,
    `test_hosted_data`, `test_two_organizations` with a set per organization, `test_memberships`,
    `test_origin`, `test_shipped`, `test_limits`, and after review `test_landing`,
    `test_key_presets`, `test_rotation`, `test_capability`) run with the feature on and assert
    it; threat model I13.
  - [x] **11h** (#231, merged 2026-10-09) Store II: sampling, assignment, blind labelling, adjudication,
    agreement (the store and the key holder's command line; no route, so no person labels yet):
    `mendari evalset sample` (seeded within strata, each drawn case's weight sealed),
    `assign` and `adjudicate` (two labellers a case, balanced across labellers and pairs, and a
    third for a disputed one; never an administrator of the set's organization; an assignment
    whose membership ended moves on), `progress` (counts, no case named, no key), and
    `agreement` and `coverage` (measured, sealed as the set's aggregates, chained); in the store,
    `next_case` (the input and the questions only, the read chained first) and `record_label`
    (one sealed label per assignment, bound to its id, chained as a salted commitment; agreed,
    disputed, resolved or unresolved). Tables `evalset_assignments` and `evalset_labels`; five
    new events; `audit verify --evalsets` recomputes labels; `SEALED_COLUMNS` covers labels;
    the 11g sweep plants labels and labellers with and without assignments (`evalset agreement`
    and `evalset coverage`, which count by label, are the only commands that may name a label
    word); the runbook's "Labelling a set".
  - [x] **11i** (#233, merged 2026-10-09) Store III: freeze and its commitment, the grading read, the rotation
    rule, retirement (the store and the key holder's command line; no route, so nothing grades
    yet): every write refuses a set that is not a draft, in its own exclusive transaction;
    `mendari evalset freeze --max-runs N --max-age-days D [--drop-unsampled]` (every drawn case
    agreed or resolved, an undrawn one removed and chained; the content `mendari.evalset/1`
    hashed with the derived labels, the rule and its freeze time; the salt, the content hash
    and the derived labels sealed as the freeze record, the final labels' coverage and agreement
    as the aggregates; only the salted commitment chained, `evalset.frozen`; each critical
    label's distinct positives printed, a warning under 100), `retire` (the content checked once
    more, `evalset.retired`, nothing published) and `show` (state, commitment, runs used and
    left, the stored aggregates; read-only, no key needed); in the store, `grade_read` (a live
    grading key of the set's organization, the rule by chained runs and age with an injectable
    clock, the content recomputed, a frozen set activated, `evalset.activated`, and each read
    chained, `evalset.graded`, in one exclusive transaction before the in-process run receives
    the cases; only the run's answer returned, nothing stored). `audit verify --evalsets` and
    `rotate --check` recompute every set the chain froze against its commitment, and `verify`
    checks its states against their events; four new events; the 11g sweep gains frozen,
    active and retired sets (`evalset freeze` and `evalset show` join the commands that may
    name a label word, conflict 57 amended); threat model T6; the runbook's freezing, rotation
    rule, grading, retiring and second lifecycle.
  - [x] **11j** (#238, merged 2026-10-10) The labelling routes (`label`, memberships mode with both keys; the
    hosted site, without the keys, and in claims mode until 2026-11-07, mounts none): `GET /api/labelling` (the
    labeller's own counts, set by set), `GET /api/labelling/next` (their oldest open case: the
    input and the set's questions only, the read chained first; `204` when nothing waits) and
    `POST /api/labelling/{assignment_id}` (`{labels}` answered with `{remaining}`); an unknown,
    another's, another organization's or closed assignment is one `404` (`NoAssignment`), a
    `422` names the question, never the value (`LabelError`); closed answer models; one `*`
    rule under `/api/labelling` before the `GET ^/api/` catch-all, its paths `admin` in claims
    mode (`_MEMBERSHIPS_ONLY`); a `label` rate-limit class (60/min, burst 30, per labeller); not
    notice-gated; security-log events `evalset.case.read` and `evalset.labelled` with set ids
    only; the `Labelling` OpenAPI tag and the `/docs` table's `label` row; in the store,
    `labelling` (the counts) and `assignment_set` (the set the log names). Registered in
    `test_auth` (pins, and no evaluation-set route resolves to `read` or `discover`),
    `test_shipped`, `test_two_organizations` (a labeller refused the other's assignments, served
    their own), `test_memberships`, `test_limits` (both account modes), `test_security_log`,
    `test_landing` and the 11g sweep, which labels through the routes and allows a case's input
    only in a labeller's own next case; `evalsets_are_on` asserts the routes in memberships
    mode; the runbook's "How a labeller labels".
  - [x] **11k** (#248, merged 2026-10-16) The set-management routes (`curate`, mounted as 11j's: memberships
    mode with both keys): `GET /api/evalsets` (the organization's sets: id, name, pack, state,
    when made, cases), `GET /api/evalsets/{set_id}` (name, pack and questions; state,
    commitment, rule, runs used and left; cases by status and each labeller's work by user id;
    the stored aggregates with `measured_at`, null before `evalset agreement`/`coverage` or
    freeze) and `POST /api/evalsets/{set_id}/sample`, `/assignments` and `/adjudication`
    (`evalset.sampled`, `evalset.assigned`; counts only); another organization's set, an
    unknown and a malformed id are one `404` (`NoEvalset`, `NO_SET`, naming no id), decided in
    the store (`org` on `sample`, `assign`, `adjudicate`, `show` and `progress`; new
    `named_sets` and `summary`); closed answer models pinned field by field down to the
    aggregates; one `*` rule under `/api/evalsets` before the catch-all, its paths `admin` in
    claims mode; no rate-limit class (an administrator's writes); security-log events
    `evalset.sampled`, `evalset.assigned` and `evalset.adjudication.assigned` with set ids only
    (`no_set` refusals with none); the `Evaluation sets` OpenAPI tag and the `/docs` `curate`
    row. Registered in `test_auth`, `test_shipped`, `test_two_organizations` (each
    administrator refused the other's set and labellers, served its own), `test_memberships`,
    `test_limits`, `test_security_log`, `test_landing`, `test_hosted_data` and the 11g sweep,
    whose administrators draw, assign, adjudicate and read through the routes (conflict 57
    amended: a set's answer to its curators may name label words); threat model I13 and E11;
    the runbook's "Curating from the dashboard API".
  - [x] **11l** (#250, merged 2026-10-16) The grading route and the aggregates-only result (`grade`):
    `POST /api/evalsets/{set_id}/runs` (`{pack, variants?}`), mounted with both keys in both
    account modes (its path out of `_MEMBERSHIPS_ONLY`, its `*` rule before the curators'); only
    an organization's grading key passes, for its own sets (another's, an unknown and a malformed
    id are one `404`, `no_set`); the set's header checked against the pack asked for and every
    slot's variant checked local before any case is read (conflict 52); then `grade_read` (its
    refusals now `GradingRefused` with fixed reasons, a `receipt` of what it chained,
    `GradingRead`) hands the cases in memory to a Decision with no store, and the in-memory
    `mendari.eval/2` result is projected onto the closed `mendari.hidden_result/1`
    (`evals/hidden.py`, from `share.py`'s models: aggregates only, calibration bins and Brier
    intervals over `MIN_GROUP` cases only); nothing stored; refusals and failures answered with
    a fixed `reason` and sentence (`GRADING_REFUSALS`), `409` `set_changed` with an operator's
    log line naming the set only; `grade` counts as `evaluate`; security-log event
    `evalset.graded` with set ids only; the `Grading` OpenAPI tag and the `/docs` `grade` row; in
    the store, `grading_header` and `pack_problem` (public). `tests/test_evalset_grading.py`
    (both account modes where it matters, SQLite and Postgres). Registered in `test_auth`
    (pins; claims mode reads the route as `grade`), `test_shipped` (`CHAINED`), `test_limits`
    (both modes), `test_two_organizations` (a frozen set and a grading key per organization, in
    both modes: refused the other's, served its own; `secrets_of`), `test_landing` (the `grade`
    row), `test_memberships`, `test_evalset_routes` and `evalsets_are_on` (the route in either
    mode); the 11g sweep finds the route by itself and grades each organization's frozen set
    through it in both modes, the grading key's answer holding no mark (conflict 35; conflict 57
    amended: label words there, as aggregates); threat model I13 and E11; the runbook's
    "Grading a hidden set"; the spec's `mendari.hidden_result/1`.
  - [x] **11m** (#241, merged 2026-10-10) The labelling screen (`#labelling`, `dashboard/labelling.js`, over
    11j's routes only): shown to a person holding `label` where the routes are served (main.js
    reads their counts once to know; without the keys a labeller-only person keeps the access
    page, now saying labelling is not switched on); a labeller and nothing else sees it alone
    (`labelOnly`, pinned in `route()` as `launchOnly` is; no packs, no notice, no review count),
    anyone else holding `label` gets its link in the section list. Their counts set by set (ids
    only), then one case at a time: its input escaped by `record.js` (the generic renderer taken
    out of `pack.js`, which imports it), the set's questions as radio groups with nothing chosen
    (24 px targets, 44 on touch), Save → the next case with focus on its heading; an unanswered
    question or a `422` said beside the question it names; a `404` moves on to the next; `204` the
    empty state with "Check again". Imports only `router.js`, `ui.js` and `record.js`; nothing in
    the browser's storage; tokens only. `tests/browser/test_labelling.py` (a real memberships
    server with throwaway keys: labels case after case, the second labeller blind, `pins()`,
    storage empty, unanswered, `422`, `404`, other roles' link, keys off, axe in both themes,
    phone and `UNDER_24`, the import lines pinned); `test_access`'s labeller case updated. No
    gate probe changes (11o replaces `labelling-underway`'s).
  - [x] **11n** (#251, merged 2026-10-17) The management screen (`#evalsets[/<set id>[/<tab>]]`,
    `dashboard/evalsets.js`, "Hidden sets" in the section list, over 11k's routes and the members
    list only): available, link included, only to a signed-in person holding `curate` in
    memberships mode where `/api/health` names the evaluation-set keys (`state.evalsets`, read
    at boot: no extra request); labellers, other roles, claims mode and a deployment without the
    keys never see it and ask for no set. The list (name, id, pack, state, cases), then a set's
    facts (state as Draft, Frozen, In use or Retired; commitment; evaluation runs used and left)
    and tabs copied from admin.js's pattern: Progress (cases by status, first and third labels
    by labeller, named from the members list, read again on each tab), Sample, Assign and
    Adjudicate (a draft's only; seeded; only labellers holding no curating role offered; a
    refusal said in the form), Agreement (percent agreement k of n through `figureHtml`, kappa
    and alpha as numbers or "not estimable" with the reason) and Coverage (stats, warnings,
    critical labels with the evidence mark, labels, mix); "Not measured yet" until the key
    holder stores them; an unknown or another organization's set "No such set". Tokens only;
    single-line imports of `router.js`, `table.js` and `ui.js`. `tests/browser/test_evalsets.py`
    (draw, assign, adjudicate and follow; refusals, each describing the fields it marks; a
    draw's button busy until the set is read again; the set the address names, by its latest
    read; aggregates in words and `BARE_PERCENT`; a frozen set, and one frozen while open;
    another organization's set, and `.`; `pins()` for a labeller, every other membership role
    but an administrator's, a labeller who also reads, an administrator without the keys and one
    in claims mode; axe in both themes; phone and `UNDER_24` on every tab; the import lines)
    and `test_invite_page` (the screen's labeller rule and evidence threshold are the
    server's). The Access tab lists are unchanged.
  - [x] **11o** (#254) Close-out, tests and docs only: G2-3's `labelling-underway` and
    G3-4's `hidden-sets-frozen` lose their `pending` probes for `tests` probes (29 and 21 tests,
    named without their cases: the 11g, 11h, 11j, 11k, 11m and 11n tests, and the 11a, 11g, 11i,
    11k, 11l and 11n tests, each with the end-to-end test; browser tests included), both
    attestations left empty and no prerequisite added. The threat model's residual risks: the
    key holder decrypts offline; an administrator links one case's events by its keyed hash and
    sees who read and labelled it, and the chain keeps those user ids; adjudication shows which
    cases and pairs disagreed; until a set freezes, no commitment covers its header, its cases'
    statuses or their weights (a status edited by SQL passes verify and is frozen as it is), and
    none covers a set's stored aggregates, so a gate's positives come from `freeze`'s output;
    paraphrased public cases pass the public-source rule. The
    runbook's owner's steps (both keys made with the command, kept off the laptop, Production
    only and why, redeploy and the health check, the `0600` file, what claims mode mounts, a
    lost key, the grading key's `--days` and expiry, each set's rotation numbers, how to fill
    both attestations) and "A set from start to end", each checked against the code (every
    writing command, not only `create`, `add` and `remove-case`, refuses a database no server
    used). ADR-0011's status (built 11a to 11o; accepted once the owner's decisions below confirm
    or change its defaults), its 11m and 11o steps and its consequences; the README and the
    CHANGELOG (11m's entry, which #241 lacked, and the close-out).
    `tests/test_evalset_end_to_end.py` (SQLite and Postgres): two organizations made on the
    command line, the deployment in use with the keys on, a set made and filled with `mendari
    evalset create` and `add`, drawn, assigned and adjudicated through the curators' routes,
    labelled blind through the labellers' (one case disputed, then resolved) and followed with
    `progress`, measured (`agreement`, `coverage`), frozen, shown and retired by the runbook's
    lines, graded once through the grading route with a key and a body as the runbook writes
    them (the other organization's key answered `no_set`; a second run after retiring
    `not_gradable`), then `mendari audit verify --evalsets` passes, `rotate --check` is ready and
    the set's chain holds its events in the lifecycle's order; no answer, log line or command
    output holds a case's input, author or author's id but each labeller's own next case its
    input. The review packet (`docs/security/review-packet.md`) is not updated: neither the
    plan's 11o nor LP-12's close-out touched it.
  - [x] **11p** (#260) What 11o's review found nothing checked, now covered: each draw chains
    a commitment to every drawn case's weight by its keyed hash (`weight_refs` on
    `evalset.sampled`, `weight_ref`: a salt new at each draw, sealed beside the weight, so no
    commitment repeats and the chain does not show whose weight changed), and takes as drawn
    before what the latest draw committed to, never a status; each store of a set's aggregates
    chains a commitment to them (`aggregates_ref` on `evalset.measured` and `evalset.frozen`,
    the salt sealed with them). A case's status, clear text, is checked against its labels and
    the latest draw (`given_statuses`, sharing `_settle`'s rule), each label in the round and by
    the labeller its `evalset.labelled` chained (`_chain`, `_as_chained`: no assignment row is
    read, and every chained label must be there), and an adjudicator's label settles only a case
    whose two first-pass labels differ. `freeze` and `measure` refuse a set whose labels,
    statuses or weights do not hold, naming no case, once read again under the chain's lock (a
    write landing as they read is "run it again", `_refuse_unless_changed`); `assign` and
    `adjudicate`, still opening nothing (I13), refuse a status the chain's draw and labels'
    rounds do not allow (`_shape_mismatches`; an agreed case edited to `disputed` still reaches
    an adjudicator, whose label then settles nothing, so it stays found); `show`, and so the
    curators' `GET /api/evalsets/{set_id}` (a `400`) and `mendari evalset show`, refuse
    aggregates their commitment does not cover (re-sealed, put back, removed, or stored without
    their event),
    naming the set only, read in one statement with their latest event; `mendari audit verify
    --evalsets` breaks at the case's `evalset.case_added` (a status or a stray weight), the draw
    (a drawn case's weight) or the storing event (aggregates), at a frozen set's
    `evalset.frozen` for its rows, counts aggregates no event stored `unchained`, and excuses a
    draw, a label or a measure chained while it reads; `rotate --check` lists `status`,
    `weight` and `aggregates` failures (and `labels`). Rows from before these commitments verify
    as before and are counted (`predating`, in both answers, a count only): statuses still
    checked against labels, weights, an unlabelled case's draw and aggregates taken as they are.
    A draft's header stays uncovered until it freezes, and an open assignment ended by SQL looks
    like a withdrawal. `tests/test_evalset_commitments.py` (SQLite and Postgres; each edit fails
    on the code before it, the review's probes among them, the old chain rebuilt without the
    commitments); the payload, count and prefix pins, and the 11g sweep's payload keys and
    salts, updated. The threat model's residual bullets (what is covered; a draw names each
    drawn case) and T6, ADR-0011 (11p step and consequences), the runbook (events, drawing,
    assigning, adjudicating, measuring, freezing, `show`, Verifying, the check, the gates), the
    sample route's description, the README and the CHANGELOG.

  **Owner decisions** (none blocked the build; the hosted site, in memberships mode since
  2026-11-07, mounts no labelling or curating route until the evaluation-set keys are set).
  Open when written: how labellers reach the hosted site (memberships mode, or `labeller` in
  claims mode, and past Vercel Authentication); each set's rotation numbers (maximum grading
  runs and age, both required at freeze); what settles a three-way adjudication split; how a
  hidden case is erased (before B20). None of these has a default. Confirmed by the owner on
  2026-11-07 (in chat: "the patent gate is clear and I approve everything on my part"), each its
  default: remote providers do not grade hidden sets
  (local only under method 1.0, LP-17 decision 2); distinct positives in the evidence rule
  (LP-17 decision 3); an administrator does not also label. Later that day the owner picked:
  labellers reach the hosted site in memberships mode (decision 1; the switch is LP-12's, below,
  done 2026-11-07, and how labellers get past Vercel Authentication is not answered yet; the
  labelling routes mount only once the evaluation-set keys are set too); each set's rotation
  numbers, at most 20 grading runs and 180 days (decision 2, = LP-17 decision 4), given at each
  freeze; a clinical lead's call settles a three-way split (decision 3, to build: until then a
  split still blocks freeze until the case is removed); and adjudicators label blind, not seeing
  the two labels, as built (conflict 40; the owner: "Label blinds"). How a hidden case is erased
  (decision 7) stays open, so ADR-0011 stays Proposed.
- [x] **LP-12** Accounts, memberships and invitations (ADR-0008, Accepted 2026-11-07; built 2026-10-08, G2-3's a6-accounts prerequisite met; the hosted site runs memberships mode since 2026-11-07). Planned by a mapping
  workflow as eleven PRs, each leaving claims mode exactly as it is:
  - [x] **12a + 12b** (#159) This entry and ADR-0008; tenancy fails closed: for a signed-in
    principal of no organization (`tenancy.acting_for_nobody`), every read and write that
    resolves its organization through `tenancy.current()` is refused (`NoOrganization`, 403),
    never widened to all of them (None) or to `default`; an empty organization is one, never
    "any", in the key and client store.
  - [x] **12c** (#172, merged 2026-10-07) Decide-scoped credentials list packs and read CDS discovery (ADR-0004).
    **Owner decision (2026-10-07): approved, narrow.** The decide scope implies a read-only
    `discover` permission that reaches only `GET /api/packs` and `GET /cds-services`, each listing
    only the packs the credential may decide with (today every served pack; after LP-20 never
    another organization's). Pack detail, examples, datasets, the OpenAPI document and audit stay
    closed to scoped credentials; every role holds `discover`, an EHR launch does not; no new scope
    URI. The 12c PR adds the ADR-0004 amendment and ADR-0008's matching wording.
  - [x] **12d** (#162) User, organization, membership, invitation and request records, each
    write chaining ids and digests only; the operator's `mendari access` commands
    (organizations, bootstrap, invite, members, requests, and the read-only preflight
    `check`); runbook `docs/operations/accounts.md`. No route reads them yet. Settled in its
    review: the bootstrap's membership stays pending until the owner signs in (it never picks
    a stored person by address); an address is ASCII, lower-cased, never case-folded; only an
    administrator who has signed in keeps an organization administered; the commands that
    write refuse a protected database without its input-hash key; the preflight counts
    evaluation reports and names an id no record can have (`"recordable": false`).
  - [x] **12e** `MENDARI_ACCOUNTS=memberships`, in two PRs.
    - [x] **12e-i** (#163, merged 2026-10-07) The server: sign-in by records (the person recorded only when a
      membership waits for their verified address, they hold one, or were recorded before;
      what waits bound; one membership selected), a sealed `member` session holding the
      chosen organization and membership, never roles (read each request); one check in the
      guard refusing a principal of no organization (`NoOrganization`, 403) before every
      route but public ones, pages and a person's own two (`POST /api/auth/organization`,
      `POST /api/auth/access-requests`), and the guard acting for nobody; the window pin
      (`X-Mendari-Org`, 409); keys, clients and launches of an organization with no record
      refused; `mendari smart check` in memberships mode. Claims mode unchanged (acceptance
      test, and a matrix: a member is let through exactly as a claims person of the role).
    - [x] **12e-ii** (#167, merged 2026-10-07) The dashboard: the access page (`access.js`: the request form,
      the chooser whenever a person holds a membership and none is chosen, roles that grant
      nothing), the switcher, every request naming the window's organization, the
      access-ended and window-moved banners, "Use another account"; browser tests.
  - [x] **12f** Invitations and member management (no e-mail sent), in two PRs, so the server
    does not wait on 12e-ii's dashboard files:
    - [x] **12f-i** (#169, merged 2026-10-07) The server, memberships mode only: an administrator's routes
      (people with `keys`, never a key) to invite (`POST /api/auth/invitations`, the link
      `/invite#<token>` shown once, relative, never built from `Host`), list and revoke
      invitations (each with its `status`), list members, replace a member's roles
      (`PUT …/members/{id}/roles`), remove a member, and grant or refuse a request to join; a
      person's own third route, `POST /api/auth/invitations/accept` (the token in the body,
      the `signin` limit, the session moved to the new organization only when it acts for
      none). Every call acts on the caller's organization only; the chain names the
      administrator by id. Fixed in 12d's store: a first-time invitee is recorded in the same
      transaction (it was a 500); `now` is read under the lock and the accept is a conditional
      update (six concurrent accepts make one membership, both databases); a person's open
      request closes (`joined`, chained) when they join any other way. In claims mode the new
      paths read `admin`, as 12e-i's do. The other session's 12f critique found the last three.
      Found in review: removing a member revokes the organization's open invitations made out
      to their address (one refused as `member` stayed open, and let them straight back in);
      the administrators' security-log lines are pinned by a test.
    - [x] **12f-ii** (#177, merged 2026-10-08) The dashboard and the invitation page. `/invite` (memberships
      mode only, 404 otherwise) is public and static, never a redirect: a hashed inline script
      takes the token from the fragment and clears the address before anything else loads; the
      token waits in this tab's `sessionStorage` through sign-in (a button, `next=/invite`
      literally) and is sent only when the person presses Accept, then forgotten; every answer
      says what to do (expired, used or revoked; another address, with "Use another account";
      unverified; already a member; joined, with Switch when the browser acts for another
      organization). The administrators' Access view (`#admin/members|invitations|requests`,
      people with `keys` in memberships mode, absent otherwise: `views.admin.available()`):
      members with their roles, change and remove with a confirmation, the last-admin refusal
      in words; invite with the link shown once in a dialog (copy, never shown again); the
      invitations with their status and revoke; grant (with roles) or refuse a request. 32
      browser tests as `pytest --collect-only` counts them (25 on a real memberships server, one
      a real sign-in through another origin that never sees the token, and 7 mocked over the
      open server; axe in both themes; a phone's width) and 9 for the page's serving.
      Fixed in 12f-i: a refusal's body names its `reason` (the page read the
      wording), and removing a pending membership revokes the invitations to its address too.
  - [x] **12g** The organization bound into each chained event (memberships mode), checked by
    `verify`; per-organization administrative reader and export; the generated two-organization
    isolation test with its reviewed allow-list.
    - [x] **12g-i** (#182, merged 2026-10-08) The binding and its check. A memberships-mode server marks the
      database (`org_bound`, a settings row); every store that opens a marked one, the command
      line's included, names in each event the organization of the row it is about, read in
      the same transaction (`_bound`; the 14 kinds of before only when marked, every other kind
      but the deployment's always, which `_append_many` enforces). `verify` checks it by kind
      (`DECISION_KINDS`, `ADMINISTRATIVE_KINDS` with `SUBJECT_TABLES`; a person's, the
      deployment's and unknown kinds unchecked, so a rollback never fails on a later release's)
      and answers `"reason": "organization"` at the first event whose row was moved or deleted.
      The answers of a review and a rating are unchanged (only the chained copy is bound); the
      previous release's `verify` passes a bound chain.
    - [x] **12g-ii** (#183, merged 2026-10-08) An organization's administrative events: `GET
      /api/audit/administrative` (paged by `(subject, hash)`, never `seq`; `kind`, `subject`,
      `since` / `until` as instants) and its FHIR export (AuditEvents of ids and codes, a new
      CodeSystem and the `audit-subject` NamingSystem), administrators only, attributed through
      each row the organization owns (`SUBJECT_TABLES`), fail closed on a payload naming another;
      `mendari audit export --administrative [--org]`. Memberships mode leaves the deployment's
      views to the operator (decision 5): verify and deployments `404`, health without totals,
      a deployment hold without its reason and setter; the dashboard follows.
    - [x] **12g-iii** (#186, merged 2026-10-08) The isolation test (`tests/test_two_organizations.py`): two organizations,
      each with people of every role, API keys, Backend Services clients signed with its own key
      and an EHR launch from its own tenant, seed through the API with a marker in every free
      text; then every mounted route is called with the other's live ids (refused as an unknown
      id is, nothing written) and with its own (served). Every answer is scanned for the other's
      markers, ids, secrets and event hashes (echoes and the reviewed `FIELDS` taken out); reads
      answer the same before and after the other works; every event chained is the caller's;
      no signed-in call reads as every organization (probes on tenancy and on the store); a
      route nothing calls and the allow-list (`DEPLOYMENT_WIDE`) does not name fails it. From
      phase 2 every call also names the other organization in its query and body; a person of
      both acts as the membership chosen; an automated decision runs on beside the other's hold;
      every page is read. Both account modes, SQLite and Postgres; each of nineteen isolation
      mutants fails it.
  - [x] **12h** (#181, merged 2026-10-08) The Access view's API keys and Clients tabs, for people holding
    `keys`, in claims mode too (the people tabs stay memberships-only): a key made from a preset
    (the table drives role, scope and days, what it reaches and its guide) or by hand, its secret
    shown once (the invitation link's dialog), rotate with an overlap, revoke, history; a SMART
    Backend Services client registered from a JWK Set or a PEM public key (made a JWK in the
    browser, its `kid` the RFC 7638 thumbprint; a private key refused before anything is sent),
    its key set replaced (`POST /api/auth/clients/{id}/keys`: same id, its tokens dropped, and a
    `keys_version` stamped on each token refuses one whose assertion was checked against the old
    set, the race a DELETE alone leaves), revoked, its history. Credential history
    (`GET …/keys/{id}/history`, `…/clients/{id}/history`) reads one subject's chain through its
    row's organization (`SUBJECT_TABLES`, `subject_history`, agreed with the LP-12g session as
    the one registry its verify and organization reader extend); another organization's is a
    404 naming no id. Key status worked out by the server; the clients' `keys_replaced_at`; the
    signed-in health's `backend_services`.
  - [x] **12i** Key presets in one table (API, screen, docs, doc-snippet checks).
    - [x] **12i-i** (#176, merged 2026-10-08) The table (`server/presets.py`: rest, fhir, ai-guard, cds-hooks,
      da-vinci, outcomes, fhir-reader; the FHIR ones name their types, never `system/*`),
      `preset` on `POST /api/auth/keys` (with the preset's role and no scope; a label stored
      and chained, never read when the key is used), `GET /api/auth/keys/presets` (each one's
      reach computed from the guard's rules), the guide's table, the doc-snippet pass per
      preset. The screen is 12h's.
  - [x] **12j** (#168, merged 2026-10-07) A daily cap per principal (`MENDARI_PRINCIPAL_DAILY_LIMIT`, off
    unless set, sign-in only) on the decisions it asks for (one per request that decides, one
    per CDS order hook draft order), per local day, after the rate limits admit them; past it
    `429` with `Retry-After` until midnight, before any work; `/api/health` shows a principal
    its own. Its value is the owner's.
  - [x] **12k** (#193) Close-out: G2-3's a6-accounts prerequisite and its probes
    (`launch/gates.yaml`: 20 tests, 130 with their cases, on SQLite, Postgres and in the
    browser); the two acceptance lines that had no test of their own, `mendari audit verify`
    after membership, invitation and key events, and an empty or `default` organization claim
    placing no one in memberships mode; the README; and ADR-0008's status (built; accepted once
    the owner's decisions below confirm or change its defaults).

  **Conflicts written before building, and the default each PR follows** (each confirmed by the
  owner on 2026-11-07, in chat: "the patent gate is clear and I approve everything on my part";
  each default is named by the one PR that depends on it):
  1. "An empty claim or the literal `default` gets no organization" applies to memberships mode
     only: claims mode, and production (Google, no organization claim), land in `default`.
  2. The reserved roles (`pack_author`, `labeller`, `grading`) do not join the claims-mode
     roles; a membership may hold the first two (granting nothing until LP-20 and LP-11), and
     no membership holds `grading`. If LP-11 lands first with its own names, LP-12 reuses them.
  3. Adding the organization to the payloads of event kinds that lack it (decision events and
     others) is memberships-only; the kinds that already carry it (governance, holds, keys,
     clients, evaluations, notice acknowledgements) keep it; new kinds always carry it.
  4. Rotating a Backend Services client replaces its key set (same client id, tokens dropped).
  5. Deployment-wide views (whole-chain verify, totals, deployment-wide holds) are the
     operator's: by default the command line only.
  6. The discovery fix (12c) changes claims mode too, production included: a decide-scoped
     credential lists packs and reads CDS discovery, and `/api/auth/me` lists `discover`
     (approved by the owner 2026-10-07, narrow: see 12c).
  7. The claims-mode no-role page stays as it is.
  8. Additive claims-visible changes count as "claims mode behaves as before".
  9. (12f-i) The chain names the acting administrator by id (`user:…`), never by address: the
     key routes chain `principal.actor`, the person's verified e-mail; the LP-12 events hold ids
     and digests only. The key routes are unchanged.
  10. (12f-i) In claims mode, `GET /api/auth/{members,invitations,access-requests}` read
     `admin` (a viewer gets 403 "may not admin", an admin 404) where they read `read` (404 to
     a viewer): deny by default on paths that serve nothing in claims mode.
  11. (12f-i) Accepting an invitation to an organization one is already a member of is refused
     (409), nothing written and the link left open; 12d used it up and kept the roles. Roles
     change only through an administrator. The other choice: add the invitation's roles.
  12. (12f-i) `default`'s administrators may invite, like any organization's.
  13. (12f-i) The notice gate covers inviting and handling requests (a `keys` POST is
     "minting": giving someone access is); accepting is not gated, the new member acknowledges
     before deciding.
  14. (12f-i) Removing or demoting an administrator does not revoke the invitations they made
     (their keys keep working too): they stay valid until revoked, and the list names who made
     each. The other choice: revoke them in the same transaction.
  15. (12f-i) `/invite#<token>` has no page until 12f-ii; nobody follows it before then (the
     hosted site stays in claims mode until after 12g). 12f-ii serves `/invite` as a public,
     static page, or drops the fragment before sending a signed-out browser to sign in: a
     redirect keeps the fragment (RFC 9110 §10.2.2) and would carry the token to the identity
     provider.
  16. (12f-ii) `/invite` is mounted in memberships mode only; claims and open mode answer 404
     (deny by default, as 10; claims mode as before). The other choice: a page in every mode
     saying invitations are not used there.
  17. (12f-ii) The token waits in `sessionStorage` (this tab, this origin), never in
     `localStorage`, a cookie, a query or `next`. A link opened on another host name than
     `MENDARI_PUBLIC_URL` loses it at sign-in, and the page says to open the link again. The
     other choice, carrying it in the pending sign-in row, puts the token in server state.
  18. (12f-ii) Accepting happens on a press, never on load: there is no preview route, and a
     link scanner or prefetcher would otherwise use the link up.
  19. (12f-ii) A signed-out invitee signs in by pressing a button, never automatically, so a
     failed sign-in never loops.
  20. (12f-ii) A refusal of an invitation carries its `reason` in the body (additive to 12f-i's
     API), which the page reads instead of its wording.
  21. (12f-ii) The administrators' view is `#admin/<tab>`, labelled "Access", as `#access` is
     the no-access page; 12h adds its keys and clients tabs to it.
  22. (12f-ii) A view a person may not use is absent, not an empty state: `#admin` in claims or
     open mode, or for a non-administrator, lands on the overview and asks nothing.
  23. (12f-ii) The dashboard's role list is written in `ui.js` (no route serves it); a test
     keeps it equal to the server's.
  24. (12f-ii) Refusals in the dashboard carry their status and code, and a `422` reads as its
     messages (it read "[object Object]").
  25. (12f-ii) The one-time link is shown in a dialog and removed with it; there is no "show
     again": a lost link is revoked and made anew.
  26. (12f-ii) "Made by" names come from the members list; a removed member's reads "Former
     member" (their id as a tooltip).
  27. (12f-ii) Demoting or removing oneself is allowed while another administrator who signed in
     remains (the server's rule): the dialog warns, and the page reloads after.
  28. (12f-ii) Expired invitations can be revoked, as the server allows.
  29. (12f-ii) Pending memberships (an address waiting for its first sign-in) read "Not signed
     in yet" and can be changed or removed; they never count as an administrator.
  30. (12f-ii) The browser tests run on SQLite; tests/test_memberships.py keeps both databases
     covered.
  31. (12h) Credential history reads one subject's chain (`key:<id>`, `client:<id>`), oldest
     first: the 200 newest events, with `truncated`; attributed through the row's organization
     (ADR-0008), so another organization's is a 404. No `seq` or hashes until the owner decides.
  32. (12h) A rotated key's history is its own; the old and the new name each other (`replaces`,
     `replaced_by`). The other choice: one lineage view.
  33. (12h) Replacing a client's key set is a POST (the notice gate counts it as minting): same
     client id, role and scopes, its tokens dropped at once, no overlap (a gapless switch is two
     replacements, the old and new keys and then the new alone); sending the same set only drops
     the tokens; its daily cap and rate buckets carry across; changing scopes is a new client.
  34. (12h) `client.keys_replaced` always carries the organization, both key summaries (ids and
     thumbprints), the count of tokens dropped and `replaced_by` as the actor, as the client's
     other events name it (conflict 9 covers the LP-12 records, not the credential events). The
     other choice: a `user:` id.
  35. (12h) "Last used" is not shown: nothing records it, and recording it is a write on every
     request. The other choice: a day column written at most once a day per key.
  36. (12h) Key status (`active`, `replaced`, `expired`, `revoked`) is worked out by the server;
     the client list gains `keys_replaced_at`. Both additive.
  37. (12h) In claims mode an administrator who signed in sees the Access view with its API keys
     and Clients tabs only (ADR-0008); this amends 21 and 22. Open mode has no view.
  38. (12h) The Clients tab is absent when Backend Services is off: the dashboard reads the
     signed-in health's `backend_services` (null or `{token_endpoint}`), never a 404 probe.
  39. (12h) Scoped presets pre-fill a client's role and scope; a client stores no preset label.
     `rest` (no scope) is for keys only.
  40. (12h) A preset's guide links to the docs site (`DOCS_SITE`, kept equal to mkdocs.yml's
     `site_url` by a test); the links work once the site is published (owner step 9).
  41. (12h) "Made by" for keys and clients is the recorded actor: "You", a member's name where it
     matches (memberships mode), else the text as recorded, escaped.
  42. (12h) Revoke is offered on any key not revoked (expired ones too); rotate on an active key,
     and on a replaced one whose replacement was revoked.
  43. (12h) A pasted private key is refused in the browser before anything is sent; a PEM public
     key becomes a JWK there. The server's `check_jwks` stays the only check of what is stored.
  44. (12h) History is a dialog on a row, not a tab, and not LP-12g's organization reader (which
     extends the same store method).
  45. (12h) A new or rotated key's secret is shown once, as the invitation link is (25): only in
     the input's value, cleared when the dialog closes or the view is left.
  46. (12g-ii) A notice acknowledgement is a person's, across organizations (notices.py), so an
     organization's administrative list leaves it out; the operator's export has it.
  47. (12g-ii) In memberships mode the deployment's records (`/api/deployments`, the Decision
     Card's Deployments section) are the operator's, as accounts.md promised before 12g, and a
     hold set for every organization shows what it covers and since when, not the operator's
     reason or name. The owner may serve either to organizations instead.
  48. (12g-ii) Open mode (a local sandbox, no one signed in) reads every event on no decision,
     the deployment's own included: there the operator is the only reader.

  **Owner decisions** (none blocks a PR; each PR follows the recommended default). Confirmed
  by the owner on 2026-11-07, each its default: the owner's identity for bootstrap is the
  Google-verified e-mail, bound at first sign-in; no `seq` in an organization's views (conflict
  31); in memberships mode, keys, clients and EHR registrations of an organization with no
  record are refused; the per-principal cap stays off (`MENDARI_PRINCIPAL_DAILY_LIMIT` unset)
  until a value is set; invitations last 7 days (at most 30), and `default`'s admins may invite
  (conflict 12); the reserved role spellings (`pack_author`, `labeller`, `grading`); notice
  acknowledgement per person; the hosted site switches after 12g rather than 12f (until 12g an
  organization's administrators still see the whole-chain verify, deployment records and some
  health fields); asking to join an unknown organization gets the same answer, naming no
  organization; only people a membership or a request concerns are recorded (as 12e-i does),
  not everyone who signs in. The rest had no default; each was settled on 2026-11-07:
  - [x] Switching the hosted site (Vercel project `openhealth-hub`, Production) to memberships
    mode, and when, the owner's step: the owner chose "Now, after this PR" (2026-11-07), with
    the bootstrap address the owner's Google address already in the role map, Claude running the
    preflight and the bootstrap (`mendari access bootstrap` and `mendari access check`, in the
    order `docs/operations/accounts.md` gives), and the switch, ADR-0008's acceptance and G1-2's
    rotation attestation recorded in one PR. Done 2026-11-07 (below).
  - [x] Who the operator is: the owner, on the command line.
  - [x] What the local datasets folder may hold in memberships mode: the shipped synthetic
    examples stay, shared by every organization.
  - [x] What becomes of an organization in use whose id no record can have: moot, since the
    preflight found only `default` (recordable).

  **The switch, done 2026-11-07 (US Eastern).** The preflight, about 6:25 pm EST, from a checkout
  of main `8dcf911b` (the commit Production served): `mendari audit verify` ok, 519 events;
  `mendari access check` found only `default` (recordable, no record yet) and exited 1, as
  expected before the bootstrap. The copy of the input-hash key used for the commands matched
  production's only hash epoch (its key id checked, read-only). `mendari access bootstrap`
  created `default`'s record and a pending admin membership for the owner; `access check` then
  exited 0 (every organization in use has a record); `audit verify` ok, 521 events.
  `MENDARI_ACCOUNTS=memberships` was added for Production only and Production redeployed (about
  6:28 pm EST); the database was marked `org_bound` at the first cold start in memberships mode.
  About 7:05 pm EST the owner signed in with Google once: the membership bound (`user.recorded`,
  `membership.bound`), and the owner is admin of `default`; saving their role as admin,
  unchanged, chained a no-op `membership.roles_changed` (admin to admin). `audit verify` ok, 525
  events. `MENDARI_OIDC_ROLE_MAP` and `MENDARI_OIDC_ROLE_CLAIM` stay set: ignored in memberships
  mode, kept for a rollback. Rollback, if ever needed: remove `MENDARI_ACCOUNTS` from Production
  and redeploy (or Instant Rollback, then remove it); the records and the `org_bound` mark stay.
  ADR-0008 is Accepted (2026-11-07). Still open: how labellers get past Vercel Authentication
  (LP-11 decision 1's other half), and the hosted site's labelling routes, which mount only once
  the evaluation-set keys are set (LP-11).

- [x] **LP-18** A registry that shows only what it checked (ADR-0012, Accepted 2026-11-07;
  built with 18h, 2026-10-17; G3-4's a10-registry-hub still waits on LP-19). Planned
  (2026-10-09) as eight PRs, each merged before the next, and the Decision Card's approval
  screen added: nine items; LP-16 is merged (16a to 16g). None changes what a pack decides on its own engine; 18d only
  lowers automation for pulled packs.
  - [x] **18a** (#228, merged 2026-10-09) The card rendered by the registry and the two groups: `render_card(pack,
    report=, board=, rendered_by=)` takes its attestations as explicit inputs (default the
    pack's own, for `mendari card`); a publish stores `mendari.registry-version/2` metadata with
    the card rendered from the digested files (`rendered_by="registry"`: no validation figures,
    the risk row "declared by the publisher") and no benchmark or leaderboard; every read
    answers `checked` (signature key id, archive SHA-256, digest, engine `bounded` or
    `maintained`, `card: rendered`, the status label, `review: unreviewed`, the namespace's
    publisher type) and `claimed` (publisher, risk level `declared_by: publisher`, licence,
    summary); a version stored before serves no card, benchmark or leaderboard
    (`card: not_rendered`); the reports route no longer carries the benchmark; `mendari search`
    prints no figure and says the risk is declared. The Hub: both groups on the pack page, the
    card through `remark-gfm` 4.0.1 with `skipHtml` (tables render; a script tag, an `onerror`
    image and a `javascript:` link in publisher text are inert, `hub_seed.py
    --hostile-example`, run by `hub-site`), no benchmark fact or leaderboard. Regression test:
    AI Guard (risk 2) with an uploaded risk-1 card, a 99.9% benchmark and leaderboard shows only
    the registry's card, everywhere. ADR-0012 and this plan. Adversarial review (1 high, 2
    medium, 4 low), all fixed: publisher text (a newline or Markdown in the name, summary, card
    fields, rationale, evidence, criteria, names, or the changelog) made headings, tables,
    rules, images and "Clinically reviewed" claims in the registry's card; in that card every
    publisher string is now one line with Markdown's punctuation escaped (`_t`), every name a
    code span its own backticks and pipes cannot break (`_c`), an evidence link only an http(s)
    address with nothing Markdown reads, and the changelog's `## ` versions its only headings
    (`mendari card` on a pack of one's own is unchanged, byte for byte); a pulled pack's
    uploaded benchmark and leaderboard were still shown by the engine's pack detail and
    `mendari card` (`card_of`: a pack from a registry as the registry renders it); the Hub
    renders no image; a card over 1 MB is refused; `checked.engine_release` names the release
    the engine was chosen against, and the Hub says the publisher type is the namespace's as it
    stands now; a changelog that is not UTF-8 is refused (422), tested. Left as is: the monitor
    still compares a pulled pack's live decisions with its uploaded benchmark (a reference for
    alerts, not a figure shown as checked).
  - [x] **18b** (#229, merged 2026-10-09) The registry's chain: every write appends a chained event in its transaction
    (`hub_events`, `record_event`, hashed as the audit chain is: `namespace.created`,
    `namespace.publisher_type_set`, `token.issued` naming the token by its id, the start of its
    SHA-256, `key.added`, `pack.published` with digest, archive and key, `report.shared`,
    `pack.reviewed`), each naming who acted (`admin` or `token:<id>`); `mendari registry verify
    --db` (read-only; exit 1 when broken) checks every hash and the order, and that every
    namespace, key, token, version (with its published digest and archive), report and review
    is named by its event, so a row written past the API is found; a registry with rows from
    before the chain records them once (`registry.chain_started`). A review (`POST
    …/{version}/review`, the admin token until LP-19's reviewers: the risk level found and a
    one-line note) shows in `checked.review` (`reviewed`, the level, whether it matches) and
    `claimed.risk.confirmed`; the newest is shown; the Hub says it in words, and `hub_seed.py
    --hostile-example` records one that found a higher level. `test_shipped.py`: the registry's
    seven writes chained, `NOT_CHAINED_YET` empty and its strict xfail gone. Adversarial
    review (2 high, 3 medium, 6 low), all fixed but one low left as is: emptying the chain and
    reopening the registry whitelisted planted rows (now `hub_meta` marks a registry once, and
    an emptied chain is reported); `verify` checked only that rows were named, so a changed
    publisher type, version metadata, review, report, token namespace or key, a removed row
    and an archive's bytes went unseen (each event now holds its row's hash, rows must equal
    their newest event's both ways, archives are re-hashed and keys' ids recomputed); the
    unkeyed chain's limit is in ADR-0012, with its head published (`GET /v1/chain/head`,
    printed by `verify`); the pre-chain record hashes reviews too; reviews ordered by a stored
    ordinal; `matches_declared` null without a declared level; a note of printable characters
    only (no C1, bidirectional or separator characters), 300 at most; `report.shared` names
    who acted. Left: a review note is chained for good (an administrator's words about a pack;
    the route says never case text). A signed head, as the audit chain has (LP-21k), is
    for later.
  - [x] **18c** (#232, merged 2026-10-09) Yank as a recall, and delete (`hub/yank.py`): a `mendari.yank/1`
    record (pack, version, digest, archive, a one-line printable reason of at most 300, a
    severity `low` to `critical`, the signer, its key id, a UTC time) signed over its canonical
    JSON, by a namespace key (`mendari yank ns/name@version --reason --severity --key`,
    `RegistryClient.yank`) or, for an administrator's recall (`POST …/yank` with a reason and a
    severity), by the registry's `MENDARI_REGISTRY_SIGNING_KEY` (32 bytes base64; `GET
    /v1/registry-key`; without it 409). Stored only if it names exactly that version, is within
    10 minutes of now and its signature holds with that key; once per version (409); chained
    `pack.yanked` (`hub_yanks`, row-hashed). A recalled version leaves latest, search and the
    namespace's list (`latest` is null when every version is), stays served by exact version
    with `yank`, and `GET /v1/yanks` lists every recall; its archive is never deleted by a yank.
    Delete (`POST …/delete`, administrators, a reason): the archive answers 410 with the reason,
    its bytes go when no version that is not deleted needs them, row and tombstone kept
    (`hub_deletions`, `pack.deleted`), the version never published again; `verify` allows a
    deleted version's missing archive. `mendari pull` of a recalled exact version prints the
    record and whether its signature holds with a pinned key (`recall_of`; the registry's own
    key pinned on first use as `@registry`); latest with every version recalled is an error.
    The Hub lists recalled versions apart (severity, who signed, when, the reason as text), and
    a pack with every version recalled says so. `hub_seed.py --hostile-example` recalls an
    earlier version. Adversarial review (3 medium, 4 low), fixed: a version deleted after it
    was pulled was still served from the cache with no word (a pull now refuses it and removes
    the copy; `recall_of` treats a deletion as a recall); the yank, delete and review routes
    parsed any body before checking the token (5 MB: about 120 MB of memory; now the sender
    first, then at most 16 KB); `recall_of` took the namespace and pack from the record and
    compared with a second answer (now the record must name the pulled pack, version and
    digest), and a key this machine had not pinned aborted the pull and hid the recall (now
    `verified: false` with why); the registry's key is pinned at a pull's first contact, not
    only when its first recall is read. Left for 18d and 18e, as planned: a server, and
    `Decision.from_hub` from the cache, do not yet ask the registry (18e checks `GET
    /v1/yanks`); the registry's own key has no rotation path yet (a key history, later);
    `GET /v1/yanks` is not paged (only token holders and administrators make recalls).
  - [x] **18d** (#234, merged 2026-10-10) Origin at pull and the cap: the verified marker (`.mendari-verified`)
    is now `mendari.origin/1` JSON (registry URL, namespace, publisher type, key id, archive,
    digest, when, the recall served then), rewritten on every pull, a cached one too; one from
    before (the archive's SHA-256 alone) still reads (`core/origin.py` `pulled_record`,
    `Pack.pulled`). A pack from a registry never automates until its organization approves its
    exact digest, at every risk level (2 and 3 need their governance approval too), nor while
    its marker records a recall: `Decision._origin_holds` adds to the hold, so AUTO becomes
    HUMAN_REVIEW with the same recommendation and the reason; no field of the pack lifts it,
    and a pulled copy of a reference pack is capped too (default 5). `registry_approvals`
    (`AuditStore.approve_registry_pack`, `revoke_registry_approval`, `registry_approval`),
    `GET/POST /api/registry-approvals` and `DELETE …/{id}` (`approve`; a served pack from a
    registry and the digest it serves only), chained `registry_pack.approved` and `.revoked`
    (administrative kinds, FHIR AuditEvent, security log). `mendari decide --db` honours the
    trail's approvals and is capped without one; `mendari test`, `eval` and `leaderboard --run`
    check the pack's own policy (`origin_cap=False`), and `mendari pull` says what serving it
    needs. Adversarial review (3 medium, 7 low), fixed: an unapproved or recalled pack's own
    rule's BLOCK still acted alone (now `apply_policy(blocks_to_person=…)` sends it to a person,
    as for community packs); the server's evaluation route measured the capped pack, the CLI the
    pack's own policy (now both the pack's own); a recall issued after the last pull reaches a
    server only in 18e (the ADR says so); the recall's severity in reasons is one of the four
    (else `unknown`); the marker keeps the registry's URL without user, password or query; the
    memberships preflight counts registry approvals; one live approval a digest (409); the ADR
    says approvals do not expire and library callers are capped; tests for cross-organization
    approvals, risk 4 and the permission.
  - [x] **18e** (#236, merged 2026-10-10) A running server honours a recall (`server/recalls.py`
    `RecallWatch`): for each registry its served packs came from (the marker's URL), it reads
    `GET /v1/yanks` every `MENDARI_REGISTRY_RECALL_CHECK_S` (300 s unless set) in a thread of
    its own, https only (http to this machine), 5 s timeout, 5 MB at most; a pack it lists (id
    and version: recalled with its severity, or deleted), one whose registry was not read in
    `MENDARI_REGISTRY_RECALL_STALE_S` (24 h unless set), one never read since start, and one
    whose marker names no registry are held: AUTO becomes HUMAN_REVIEW and its own BLOCK goes
    to a person (`Decision(recall_source=)`, with the approval cap). A failed read keeps what
    was known until stale. Health (past the read check) shows `registry_recalls`: each
    registry's last read's age, how many it recalls and the error type while reads fail.
    Adversarial review (2 medium, 5 low), fixed: a read had no total time limit (a registry
    sending a byte a second held it for good, and no other read started), now the whole answer
    within 20 s and a read in flight over 60 s is taken as lost; the answer was buffered whole
    before its 5 MB cap, now refused by its length or as it streams; a thread that cannot start
    no longer fails the decision; a failed read is logged once a change of state (its type);
    the stale time must be at least twice the check time; the settings table says what a
    serverless host or `mendari decide` sees (the recall known at the last pull) and that
    these reads are not under `MENDARI_PROVIDER_ALLOWED_ORIGINS`; tests for every answer
    `fetch_yanks` refuses, the single read in flight and the settings.
  - [x] **18f** (#237, merged 2026-10-10) Rate limits and headers. The `Limiter` runs on the registry's own
    database (`core/rate_buckets.py` `RateBuckets`, moved out of `AuditStore`, which now uses it
    too: the same statements): reads per address (600/min, burst 200), writes per token or
    address (120/h, 30), site reports per token (60/h, 10), `MENDARI_REGISTRY_RATE_LIMITS`
    (`on` unless set, `log`, `off`, then overrides; a bad value stops the registry); a refusal
    is `429` with `Retry-After` before the body is read, nothing stored. Every registry answer:
    the engine's security headers, `default-src 'none'; frame-ancestors 'none'; base-uri 'none';
    form-action 'none'`, COEP `require-corp`, `Cache-Control: no-store`, and HSTS but on this
    machine (`tests/test_registry_limits.py` checks every route and method by value). The Hub
    (`hub/proxy.ts`): a CSP with a fresh nonce per page (`script-src 'self' 'nonce-…'
    'strict-dynamic'`, `frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'none'`), the
    same headers and COEP, HSTS but on this machine (`hub/tests/headers.spec.ts`: every page,
    the 404 and assets by value, each nonce fresh, the inline scripts carrying it, no
    violation). CI's `hub-site` runs the OWASP ZAP baseline (the pinned image) over the site,
    `scripts/zap_check.py` failing on medium or high: locally 0 medium or high (2 low, 4
    informational), the Spectre isolation low fixed by COEP. Adversarial review (with 18g's):
    made-up bearer tokens each had a bucket, so writes were never limited (now a token the
    registry did not issue counts against its sender's address); the limit's database work ran
    on the event loop (now in the thread pool); an unhandled error's 500 had no headers (now a
    bare 500 with them); the Hub's tests cover an unmatched page too.
  - [x] **18g** (#239, merged 2026-10-10) The regression check (`hub/regression.py`): on every publish
    (`MENDARI_REGISTRY_REGRESSION`, `on` unless set), the pack is evaluated on its own
    `evaluation.benchmark_dataset` (a file its digest covers) in a child process (`python -I -m
    mendari.hub.regression DIR`: an environment of five fixed variables, no credential or
    setting of the registry's, 180 s wall clock and CPU, 2 GB of memory where the system bounds
    it, the pack loaded from a registry on its engine, local providers only), and the pack gate
    (`evals/gate.py`, as `scripts/pack_gate.py` runs it) compares it with the newest earlier
    version's check that is not recalled. Stored with the version (the summary the gate reads:
    the set's name, size and hash, the primary accuracy over n, false automation, each critical
    label); served as `checked.regression` (label "publisher's own test set: regression check,
    not a ranking", accuracy as k of n, what it was compared with, the regressions); shown,
    never refused. Not run: `no_test_set`, `needs_remote` (a default provider calls a model),
    `failed` (the error's type, never its text). The Hub says it in the checked group; it is
    not in `figures` and nothing ranks on it. The five reference packs and the hostile example
    measure on the seeded registry (16.6 s for the seed, with the checks). Adversarial review
    (1 high, 4 medium, 6 low; with 18f's), fixed: a publish ran the check on the event loop, so
    the registry answered nothing for up to 180 s, and a re-sent existing version ran it again
    (now in the thread pool, at most 2 checks at once else 503, and an existing version is
    refused before any work); the gate used the new version's own tolerance (now the earlier
    check's); the baseline skipped recalled versions and stopped at one that measured nothing
    (now the newest measured check, recalled or not, said so), and a changed test set read as
    no regression (now "not compared"); a failure in the gate refused the publish (now said
    in the check); the child's limits are set in the child itself (no `preexec_fn`), with a
    file-size limit, its errors dropped and its answer read to 1 MB; the raw check (a bare
    accuracy) is no longer served beside `checked.regression`.
  - [x] **18d screen** (#240, merged 2026-10-10) The registry approval on the Decision Card: an approval is keyed
    on the pack and its digest (per organization), so it lives in the card's Automation approval
    section, not in Access. For a pack served from a registry: where it came from (registry,
    namespace, signing key, when pulled, the digest served), a recall recorded at pull, the
    approval in force (who, reference, when, digest) and every earlier one (revoked by whom,
    or of another version); approving takes a reference (and an approver in open mode), then a
    dialog naming the digest and saying what it allows and what it never lifts (a recall, a
    hold, the risk level's limits); revoking is one button; both only with `approve`. The
    card's "Automates" says a pack from a registry waits for this approval or its recall, and
    a level 0–1 pack from a registry no longer reads "No approval is needed". No server change.
    `tests/browser/test_registry_approvals.py`, and axe in both themes (not approved with a
    recall, the dialog open, approved). `GET /api/providers/jev/models` gets no screen: no
    place in the dashboard chooses a model (`JEV_MODEL` is the deployment's), the card already
    names the deployed model, and the list would be a call to Jev on every card for nothing to
    act on.
  - [x] **18h** (#255) Close-out. The engine no longer uses a pulled pack's uploaded
    benchmark, the gap in the acceptance check that needed code (18a left the monitor as is,
    and nothing covered the evaluation's comparison; the others are residual risks in the
    threat model): `/api/monitoring` and `mendari monitor` take no
    benchmark for a pack from a registry (`benchmark.reason: from_registry`, its figures null,
    only the checks that need none run; the dashboard says why), and the evaluation route and
    `mendari eval` attach `comparison: {status: unknown, reason}` instead of comparing with the
    benchmark it carries (`--baseline report.json` is still compared), and `mendari demo` says
    the same of a pulled copy of its pack; every other pack as before. Gate a10's LP-18 half is
    its tests, in five probes by the prompt's Tests (the card, the two groups and the engine's
    uploaded benchmark; recalls, deletions and a server honouring them; the pulled-pack cap and
    the Decision Card's approval; limits and headers, the Hub's half pinned by
    `test_the_hub_site_job_runs_the_hubs_tests_and_the_pinned_scanner` and
    `test_zap_check_fails_on_a_medium_or_high_risk_only`; plain publisher text and the
    chain); its `pending` probe names LP-19 alone, so a10 stays unmet. Threat model rows S18,
    S19, T7, R3, D11, D12, E12 and E13, with E1, E3, S4, the trust boundaries, the header and
    the Pack content part amended, and residual risks (the admin token until LP-19, unverified
    site reports, an unreadable registry, the unkeyed chain, the signing key, the regression
    check, approvals that never expire, the Hub's checks not required). ADR-0012's status
    (built; accepted once the owner confirms) and Consequences (the breaking cap).
    `docs/operations/registry.md` (in the nav), README, CHANGELOG, SECURITY.md, the
    evaluation-reports spec (the registry's `figures` always null), the review packet's scope
    (the registry and the Hub in it), architecture and the authoring guide;
    `scripts/zap_check.py`'s docstring names both jobs. The review: the registry's 500 carries
    HSTS as every answer does, and the docs say what a server's recall read is (a decision
    starts it once the last is five minutes old, and uses the list read before, so the
    first decision after a quiet spell can still automate; a hold acts within seconds).
    **LP-18 is built.**

  **Defaults each PR follows** (each confirmed by the owner on 2026-11-07, in chat: "the patent
  gate is clear and I approve everything on my part"; ADR-0012 Accepted with them):
  1. (18a) The Hub shows no benchmark figure and no leaderboard until the registry computes
     them (18g); only LP-17 ranks.
  2. (18a) Versions published before LP-18 keep their stored metadata (immutable) but serve no
     uploaded card, benchmark or leaderboard; publishing a new version renders a card.
  3. (18b) Until LP-19's reviewer accounts, a review is recorded with the admin token.
  4. (18c) A yank is signed by whoever makes it: the publisher with the namespace's key, the
     registry with its own key. A client honours every yank, verified or not (a forged yank only
     stops automation, the safe direction), and says whether it verified.
  5. (18d) A pulled copy of a reference pack is capped too: the cap follows the origin, not the
     bytes. `mendari test` checks the pack's own policy and says that serving caps it.
  6. (18e) A server that could not check a pulled pack's registry for 24 hours holds it.
  7. (18g) A regression is shown, not refused: the version is the publisher's; a pack that
     cannot be evaluated on its own test set with local providers says so.

- [ ] **LP-17** The scoreboard and the grading service (ADR-0013, Accepted 2026-11-06). Built
  but for two steps (2026-11-06, the LP-17 lane): 17i, the owner's run of the remote arms, and
  17l, the developer flow, blocked on LP-19 and LP-20 (owner decisions 4 and 10 decided
  2026-11-07); gate G3-4's `a9-scoreboard` names both as pending, and the owner's lines below
  are the runbook's steps (`docs/operations/scoreboard.md`, "The owner's runbook"). Planned by a
  mapping workflow (2026-10-17) as thirteen PRs, 17a to 17m: twelve build PRs and one data PR the owner
  runs (17i). It adds a published grading method, a scoreboard computed from ledgers
  (`mendari.scoreboard/1`), and scoped grades and band ranking given only on frozen hidden sets.
  `mendari.eval/2`, `mendari.ledger/1`, `mendari.compare/1`, `Proportion.to_dict`,
  `mendari.rank/1` (as data), `mendari.hidden_result/1`, every committed benchmark and ledger and
  every pack decision stay byte for byte; nothing is ranked or graded on a public set; CI never
  calls a live provider or holds the signing key; no PR changes a reference pack's critical-label
  figures. The full plan, its 38 conflicts with the default each PR follows, the owner decisions
  and the risks are in `notes/lp17-plan.md`.
  - [x] **17a** (#257) The grading method 1.0 (`docs/grading/method-1.0.md`,
    `mendari.grading/1.0`): status "draft, not in effect", the effective-date line left empty for
    the owner, a changelog; one section per requirement, 1 to 14; the machine-readable parameter
    block, every number "owner to confirm"; the evidence rule, `n_min(floor, L) = max(100, the
    smallest n whose n-of-n lower bound at 1 - 0.05/L reaches the floor)`, and its table for L = 1
    to 8, recomputed with `stats.clopper_pearson` (0.95: 100 up to L = 4, 104, 107, 110, 113; the
    0.02 ceiling needs 183 labelled cases); the scope statement and "engineering evaluation, not
    clinical validation". `mendari.evals.method`: `METHOD`, `METHOD_PATH`, `METHOD_SHA256` (the
    document is in neither the wheel nor the sdist, so the hash is pinned and a test checks it
    against the repository's file), `EFFECTIVE = None`, the parameters, `simultaneous_level`,
    `min_positives` and `in_effect`; it imports only `stats`, and nothing imports it yet.
    ADR-0013 (Proposed); the nav's Grading section; gate `grading-method` probes the document
    and its `pending` names the rest of LP-17 (the attestation stays empty); the spec links the
    method. `tests/test_grading_method.py`.
  - [x] **17b** (#258) Statistics additions (pure, `evals/stats.py`): the stratified bootstrap
    (macro-F1), the Korn-Graubard weighted proportion, Brier's reliability term.
  - [x] **17c** (#259) Arms by provider class (`evals/arms.py`, `mendari.arms/1`, `mendari
    scoreboard arms PACK`).
  - [x] **17d** (#261) Scoreboard entries (`mendari.scoreboard/1`, `evals/scoreboard.py`) and
    run records (`mendari.run/1`, `evals/runs.py`), pure; the price table (`mendari.prices/1`,
    refused from Mendari's code or a pack's directory) and the disclosures file
    (`mendari.disclosures/1`). An entry is a pure function of its ledger, dataset, run record,
    price table and method (`body_sha256`), refused for a ledger of another pack, dataset or
    configuration, or a run record that disagrees with its ledger (its cases, tokens or
    models). Evidence on distinct
    positives, each critical label needing `method.min_positives`; the grade's bound at
    `1 - 0.05/L` over distinct groups; every proportion k of n with its interval and every mean
    with its bootstrap interval; ECE a description; cost from tokens and the dated table, or "0
    (no metered calls)" or "unknown"; latency from the run record, never ranked; subgroups ("too
    few" under 10) and Korn-Graubard weighted figures (detection never weighted); the scope
    statement and the disclosure; a public set is a regression check with no rank and no grade.
    `board` (one table per pack and set, the banner, each arm measured, "not measured" or "not
    applicable", freshness worked out when built and never stored) and
    `render.scoreboard_markdown`. Spec sections for the three formats.
    `tests/test_scoreboard.py`. Nothing calls it yet; the store and the page are 17f's.
  - [x] **17e** (#263) Signing and the public verifier (`mendari.evals.signing`; off without
    the key). `MENDARI_GRADING_SIGNING_KEY` (Ed25519, 32 bytes in base64, the owner's, offline):
    unset is off, a malformed or reused one stops the command naming no value; it must differ from
    the checkpoint, registry, PHI (and each read key), input hash and three evaluation-set keys,
    and the evaluation-set keys refuse to equal it (`OTHER_KEYS`); the test suite removes it
    (`LIVE_SETTINGS`). A signature (`signature: {key_id, value}`) covers
    `mendari-grading-result-v1`, a zero byte and `ledger.canonical` of the result without it; it
    reads `scope.method` (version and sha256) and `scope.measured_on`. `mendari scoreboard sign
    FILE ...` refuses in CI and without the key, and with a key pinned as a publisher's;
    `mendari scoreboard verify FILE ... [--keys | --public-key]` needs no secret and names each
    refusal (another format, a body that is not canonical, a method hash not its version's, a key
    unknown, outside its dates or a publisher's, a changed byte), one line per result, a file it
    cannot read included; the keys file is read as strictly as a result. `scoreboard/keys.json`
    (`mendari.grading-keys/1`) is empty. `docs/grading/verifying.md` in the nav; threat model
    S20 and E14. `tests/test_scoreboard_signing.py`; `test_cli_errors.BROKEN` gains both
    commands, and `test_read_only_commands.FILES_ONLY` gains `verify`.
  - [x] **17f** (#267, merged with 17j in #268) The results store
    (`mendari.evals.results_store`, `scoreboard/`) and the early scoreboard (stage 3). Results named by their body's SHA-256 beside their ledgers
    and run records (each named by its own hash), append-only; `supersedes` (a new field of the
    entry's body) names an earlier result of the same pack, set, arm and configuration, one
    current result each. `mendari scoreboard run PACK [--dataset] [--config | --every-local]
    [--again]` (local configurations of maintained packs on their public scoring sets only;
    remote, `train`, `calibration`, files and hidden sets refused; a current result kept) and
    `mendari scoreboard add FILE [--supersedes HASH]` (a local configuration's result only), each
    reading the pack from the repository's `packs/`, never `MENDARI_PACKS_PATH` or the registry
    cache. `scripts/build-scoreboard.sh`: 18
    entries (every local configuration on the 15 public scoring sets), 2.7 MB, all unsigned;
    the prescreener's `structured=cql` reads "not measured yet". `scripts/check_scoreboard.py
    --base` and `scripts/scoreboard_page.py --check` are steps of the required `reference-pack`
    job (fetch-depth 0): names are hashes, nothing removed or changed but a valid signature
    added, links form a forest, current signatures verify, every public result recomputes byte
    for byte and every local ledger on its current digest re-runs (older digests recomputed and
    kept, a changed set or method kept; new results local, on the current ones and measured no
    later than today; a remote disclosure from `scoreboard/disclosures.json`). The page check
    runs before the build. A publisher's or a registry's key is refused where it is pinned (CI
    pins none: the keys file's review keeps one out). The page
    `docs/grading/scoreboard.md` (nav: "Scoreboard (public sets)"): only the Referral Router's
    `test` set has no banner (113 and 124 distinct positives). `board` gains a measured arm's
    configurations not measured yet. Runbook `docs/operations/scoreboard.md`,
    `scoreboard/README.md`, the spec's results-store section; threat model T8 and R4.
    `tests/test_scoreboard_store.py`, `tests/test_scoreboard_page.py`;
    `test_cli_errors.BROKEN` gains `scoreboard run` and `scoreboard add`. A PR that changes a
    reference pack now regenerates the page (its results show "stale: pack changed"):
    `scripts/build-reference-pack.sh` writes it last. Owner: CLAUDE.md's "Pack artifacts are
    derived" rule could name `docs/grading/scoreboard.md` and `web/index.html` among the files
    to commit. The owner said yes (2026-11-07); CLAUDE.md names both.
  - [x] **17g** (#279) Public template sets relabelled "public set: regression check, not a
    ranking" on the card, the dashboard and `web/`, with no Rank column (a display bump; the five
    cards' comparison sections become stale). The card's Provider comparison opens with the
    label, says nothing there is graded and links the scoreboard page
    (`leaderboard.SCOREBOARD`, a repository URL as the CLI's guides are); `leaderboard.markdown`
    (the card and `mendari leaderboard`) loses its Rank column, an earlier version's row saying
    so beside its configuration. The dashboard drops the "Ranked" caption, the rank column (and
    `table.js`'s rank option and its styles) and the chart's `rank === 1` naming rule (now:
    deployed, or not beaten on cost and accuracy). Its link goes to the scoreboard's runbook
    (`operations/scoreboard.md`), which links the page: the page's own path holds the word
    `tests/test_invite_page.py` bans from dashboard sources (LP-11 conflict 18). `rank` and
    `standing` stay as data and in the API. The five cards regenerated with `mendari card
    --write`; data, models, benchmarks and the page byte-identical. `web/index.html`'s typed
    table is replaced by a block between markers that `scripts/scoreboard_page.py` writes and
    `--check` checks: the inbox `test` set, every arm, the two measured entries k of n with
    intervals and marks, the banner (23 distinct urgent positives), three arms "not measured";
    no remote model named until a disclosure is stated. `tests/test_web_scores.py`,
    `tests/test_ranking.py::test_no_card_or_board_prints_a_rank_for_a_public_set`, the three
    renamed browser tests. Owner: the mendari.co entry's line about naming Jev, Claude and
    gpt-oss results no longer applies to the page as it is (it names none); it applies again
    when 17i's results are published. Lane: the page and the site's scores share one board,
    built with 17h's canaries and the disclosures file as it is now, so the site publishes an
    entry by 17h's `published`, and it reads public results only (17k).
  - [x] **17h** (#279) Remote arms on public sets: 3 to 5 replicates, the spread and the flip
    rate, model freshness, the canary and disclosures; built, and refusing to start in CI.
    `mendari scoreboard run` takes a remote configuration by hand only (refused when `CI` or
    `GITHUB_ACTIONS` is set): `--replicates 3` to `5` (a local one runs 1), `--prices FILE` when
    it calls a model (a CQL configuration calls none, so needs none), and only as its arm's
    `remote` configuration in `scoreboard/declared/<pack name>.json` (`mendari.declared/1`, per
    arm one `remote` and one `local` configuration), committed as it is before the run (git's
    `HEAD`). Replicate 1 is the headline; `inputs.replicates` and `inputs.declared` (the
    declaration's SHA-256) and `figures.replicates` (`runs.replicates`: count, the case flip rate
    k of n matched by ledger hash with its interval, each figure's spread, the models) are
    written only for an entry of replicates, so every stored result recomputes unchanged.
    `mendari scoreboard canary PACK --config ... --size 30 --seed S` (`mendari.canary/1`,
    `scoreboard/canaries/`, no schedule): drift (its lower bound above the replicates' upper
    bound) marks the arm's remote results stale (any set, measured before the canary's day or
    no later than its replicates, never a local configuration's); another model, from a canary,
    a later result of the same model type (any pack or configuration, the same day included) or
    the replicates themselves, marks every result of that type that named another. An entry with
    remote calls is published only while `scoreboard/disclosures.json` (added, empty) still
    holds, word for word, the statement each type had when it was stored; otherwise it shows its
    scope and none of its figures, and the banner leaves it out. The store check recomputes a new
    remote result from its replicates (never re-running a remote ledger), checks declarations and
    keeps canaries append-only. The prescreener's `structured=cql` measured with Mendari's own CQL
    engine (compose `fhir`), 3 replicates on each of its three public sets, 2026-11-05: 0 flips,
    "not published: disclosure pending", which they keep: once `cql` has a statement they are run
    again with `--again` (the owner line below). `tests/test_scoreboard_runs.py`;
    `test_cli_errors.BROKEN` gains `scoreboard canary`. Lane review: threat model T8 names 17h's
    rules and tests; a configuration of Jev and an LLM whose table prices only one, or whose type
    reports two models, is stored as unknown cost, not refused after every call
    (`test_a_run_of_two_model_types_its_table_cannot_price_is_stored_as_unknown`).
  - [ ] **17i** (the owner's run, data only) Remote-arm ledgers on the public `test` sets
    (31,500 decisions). The LLM arm's models are OpenAI's (the owner, 2026-11-07: "OpenAI"); the
    exact model ids, at most two, are still needed before any spend.
  - [x] **17j** (#268) Grades (`evals/grade.py`: attestations, each met, not met or
    insufficient evidence, never a letter, score or composite; draft while the method is not in
    effect) and band ranking (`evals/bands.py`), pure. The attestations: no critical case
    automated, each critical label's recall floor on its simultaneous bound over distinct
    groups, the false-automation ceiling on its 95% upper bound; insufficient when a perfect
    result could not meet one (`method.min_positives`, and `method.min_labelled`, 183 for 0.02).
    The band tuple (worst critical label's bound, false automation, accuracy, Brier, cost), each
    value read as the decimal it is written as; unranked arms with their reasons; competition
    ranks; the Holm family per pack and set (every pair by each critical label's detection,
    false automation, accuracy and Brier), named with its size; rank intervals from its
    decisions, which never reorder the ranks. Given only on frozen hidden sets, from paired
    ledgers: on a public set the ranking is off and the grade null, wired into `board` (which
    refuses a hidden table of two or more, holding no ledger). `tests/test_scoreboard_rank.py`.
  - [x] **17k** (#279) The hidden-set scoreboard through the existing grading route
    (`mendari.hidden_scoreboard/1`, every declared arm in one grading read). `GradingRequest` takes
    `arms` with `declared` (a SHA-256), never with `variants`; without them the answer is
    `mendari.hidden_result/1`, unchanged. Each declared configuration is refused before any case is
    read as `variants` is, or `wrong_arm` (new) when it is not of its arm; one grading read decides
    every declared arm on the same records (one `evalset.graded`, one of `max_runs`); entries
    (`scoreboard.hidden_entry`, distinct groups from the inputs, weights from `attributes.weight`),
    grades and the ranking in memory, never stored. The closed answer: per arm its status
    ("graded", "not graded here", "not applicable"), figures, attestations; the banner; the ranking
    ("off: one arm graded"); the scope with the set's id and commitment and the declaration's
    SHA-256; the run block outside the body's hash. No cost, latency, subgroups, equal-mass bins,
    case ids or hashes, dataset name or hash, or ledger id. A 600-case set runs in about 5 s
    through the route, its read included, so the run stays in the route. Review fixes: a declared
    arm names every slot (`wrong_arm` otherwise); the weighted figures are withheld when fewer than
    10 cases of a share carry a weight; shares are rounded to 4 places and means to 3, as
    `mendari.hidden_result/1`'s; the store checks a stored answer against its own figures
    (`hidden.scoreboard_disagreement`). The results store holds the answer as answered (closed
    schema, never recomputed, off the page). Spec section, runbooks (`evalsets.md`,
    `scoreboard.md`: post the declaration, `scoreboard add`, the owner signs), ADR-0011 notes,
    `api_docs.py`, threat model T8, I14 and I15. `tests/test_evalset_scoreboard.py`;
    `test_evalset_leaks.py` sweeps a scoreboard run and its refusal. Lane: a new answer is stored
    only when it cites its pack's declaration as the store holds it and grades the arms that
    declaration names a `local` configuration for (17h's `mendari.declared/1`), each with it
    (`test_a_stored_answer_grades_its_packs_declared_local_configuration`).
  - [ ] **17l** The developer flow: banded answers, a budget, a sealed confirmation split.
    Blocked on LP-20 (LP-19 is built; owner decisions 4 and 10 decided 2026-11-07).
  - [x] **17m** (#279) Close-out. Gate `a9-scoreboard`'s pending probe replaced by the
    acceptance tests of 17b, 17d, 17e, 17f, 17j and 17k, the no-leak tests (LP-11) and 17m's
    end-to-end test, with two pending lines left: the owner's 17i (its pattern LP-21n's) and 17l
    (after LP-20 and owner decisions 4 and 10). `grading-method`: the method's tests and
    `test_the_page_is_regenerated_from_the_store` in place of its pending line, beside the
    `file:` probe; the attestation stays empty. Threat model residual risks: a signed hidden-set
    result is the owner's attestation and no one else can recompute it (bound to its run only by
    what the chained `evalset.graded` holds); raw intervals on sets built from templates look
    narrower than they are; the key holder could sign an altered result. ADR-0013 Accepted, its
    status listing what landed, 17i and 17l open. The owner's runbook
    (`docs/operations/scoreboard.md`), each owner step once, in order, with the commands as
    `cli.py` has them: the method's numbers, the key, publishing the method, the effective date,
    the price table, the disclosures, the declarations, the replicates, the hidden sets,
    signing, publishing the scoreboard. Found while writing it: setting the effective date
    re-pins the method's hash, so a result signed before stops verifying and the store's check
    refuses it while it is current; so the runbook signs only after the effective date, and its
    signing command names the new results (`sign scoreboard/results/*.json` refuses a batch with
    one signed already or one under an earlier hash). `tests/test_scoreboard_end_to_end.py::
    test_a_local_run_to_a_signed_verified_result` (SQLite and Postgres, about 3 s each, with a
    socket guard refusing any connection off the machine): `scoreboard run`, `add`, `sign` with
    a throwaway key, `verify`, a changed figure failing `verify` and the store's check; a frozen
    hidden set graded over HTTP with the declared arm, `add`, signed and verified;
    `mendari audit verify --evalsets`. `test_grading_method.py::test_the_gate_probes_the_method_document`
    checks the gate holds no pending line. No pack, figure or decision changes.

  Order: 17a first; then 17b, 17c and 17e in parallel; 17d after 17b and 17c; 17f after 17d and
  17e; 17g and 17h after 17f; 17j after 17d; 17k after 17f and 17j; 17i after 17h; 17l after 17k
  and LP-20; 17m after 17g, 17h and 17k. Threat-model rows allocated to LP-17: S20, T8, R4, I14,
  I15 and E14.

  **Defaults each PR follows** (each confirmed by the owner on 2026-11-07, in chat: "the patent
  gate is clear and I approve everything on my part"; the plan's conflicts in full):
  1. (17d, 17g, 17j) `mendari.rank/1` stays the leaderboard's data and listing order; the
     scoreboard ranks only under `mendari.grading/1.0`, on hidden sets.
  2. (17d, 17f) Every public set is "public set: regression check, not a ranking"; the
     acceptance's "scoreboard on public sets" is the unranked early scoreboard.
  3. (17d) Each critical label needs enough evidence, not any one.
  4. (17d, 17k) Evidence on distinct positives under `norm/v1`; a group is detected only when
     every positive case in it is; figures stay raw k of n.
  5. (17a, 17d) Bounds at `1 - 0.05/L` are two-sided (conservative); display stays k of n at 95%;
     the grade's bound is its own field naming its level.
  6. (17j) The Holm tests set the rank intervals and never reorder the ranks; Brier's
     paired-bootstrap p-value joins the family; `paired.compare` does not change.
  7. (17j) Cost is banded and is the band tuple's last element.
  8. (17k) Local providers only on hidden sets: a remote arm reads "not graded here", so ranking
     is built but off.
  9. (17c, 17h) `naive_bayes` is deterministic and local, `cql` deterministic but remote; the
     specialised classifier is Jev; the "both" arm exists only across slots.
  10. (17k, 17l) Mendari's own grading declares one configuration per arm before grading; the
     salted split comes with the developer flow.
  11. (17k) Every declared arm is decided inside one grading read, one of `max_runs`.
  12. (17d, 17k) No ledger format change: weights, distinct groups, per-type tokens and latency
     come from the dataset and the run record; latency is labelled "measured on the runner, not
     reproducible" and never ranked; no subgroups on hidden sets.
  13. (17d, 17h) Price tables are dated files under `scoreboard/prices/`, cited by SHA-256; a
     cost is "unknown" when any call failed or a model is unpriced; a remote entry is withheld
     until its provider type has a disclosure.
  14. (17e, 17f) The signing key is `MENDARI_GRADING_SIGNING_KEY`, offline only, never a server
     or CI setting; committed results stay "unsigned" until the owner signs; the store is
     `scoreboard/` at the repository root, its checks steps of `reference-pack`.
  15. (17g) No dashboard scoreboard screen: the card and the dashboard link the docs page.
  16. (17a, 17m) The method stays a draft (`EFFECTIVE = None`); any edit re-pins its hash; the
     effective date is the owner's PR, at least 30 days after the URL is attested (B30).

  **Owner decisions** (17i needs 6 and 7, 17l needs 4 and 10; none blocks the others).
  **2026-11-07:** confirmed by the owner, each its stated default: nothing is built in a private
  repository (1); local providers only on hidden sets under method 1.0 (2); distinct positives
  (3); the method's numbers, the floors, the ceiling, the bands and an effective date at least
  30 days after the URL is attested (5; the date itself is still the owner's to set); two-sided
  bounds (6); the spend for 17i, 31,500 decisions once (7); the signing key made offline, its
  public key in `scoreboard/keys.json` (9); 17l waits for LP-19 and LP-20 (10); the docs site
  and the corrected `web/index.html` published after gate G1-2, with the method and the early
  scoreboard (11). Later that day the owner picked: rotation numbers and the submission budget
  (4), the suggested ones: at most 20 grading runs and 180 days per set, 3 submissions per
  organization and lineage per rotation, the confirmation once per version; and (11) waits for
  G1-2's `patent-decision` only, not all of G1-2. Still open: benchmark-publication terms and the
  commercial-relationship disclosures (8: the rule, nothing remote published without one, is
  built; the statements are the owner's to write).
  The owner's actions, one line each, in the order of the owner's runbook
  (`docs/operations/scoreboard.md`, "The owner's runbook", steps 1 to 11). The paid runs, the
  hidden-set grading and the signatures come on or after the effective date: setting it re-pins
  the method's hash, so a result signed before stops verifying, and one measured before stays a
  draft.
  - [x] Owner (decisions 3, 5 and 6): confirm or change the method's numbers (the floors, the
    ceiling, the bands, the days to stale, the replicates, the canary; two-sided bounds; distinct
    positives) before the method is published; a change is a method edit, a build PR on your
    word (step 1). Confirmed as written (owner, 2026-11-07), so no number changes; dropping the
    method's "owner to confirm" markers is a separate build PR (an edit re-pins its hash).
  - [ ] Owner (decision 9): make the grading key offline, a copy in `~/Documents/mendari-private`,
    never a server or CI setting (step 2); its public key is listed at signing (below). The
    procedure is confirmed (owner, 2026-11-07); making the key is still the owner's.
  - [ ] Owner (decision 11, after gate G1-2's `patent-decision`): publish the docs site with the
    method and the early scoreboard, and deploy `web/` (`cd web && vercel deploy --prod`); the day
    the method is at its URL starts its 30 days' notice (step 3). The order is confirmed (owner,
    2026-11-07), and it waits for `patent-decision` only, not all of G1-2 (owner, 2026-11-07; the
    mendari.co entry's lines). The owner asked Claude to run the production deploy of `web/` once
    `patent-decision` is attested and main is green: done 2026-11-07, about 6:30 pm EST, from main
    `8dcf911b` (the mendari.co entry). Publishing the docs site stays the owner's.
  - [ ] Owner (decision 5): set the method's effective date, at least 30 days after the method is
    published: the date and status lines, `EFFECTIVE` and `METHOD_SHA256` in one PR, the page
    regenerated; publish the site again; then attest `grading-method` in `launch/gates.yaml`
    (step 4).
  - [ ] Owner (decision 8): the benchmark-publication terms of Jev, Claude and gpt-oss read, and
    the commercial-relationship statement decided for each remote provider type (`jev`, `llm`,
    `cql`); until then no remote result is published (step 6).
  - [ ] Owner (decisions 6 and 7): write the price table (`scoreboard/prices/<date>.json`), the
    disclosures (`scoreboard/disclosures.json`) and the declarations
    (`scoreboard/declared/<pack name>.json`, every arm's `remote` and the deterministic arm's
    `local` configuration, committed), then run 17i on or after the effective date: one declared
    configuration per remote arm, 3 replicates, `test` sets only, 31,500 decisions, outside CI
    (steps 5 to 8). The spend is confirmed (owner, 2026-11-07), the LLM ids to be OpenAI's (17i
    above), each id still to name; the files and the run are still the owner's.
  - [ ] Owner: once `cql` has a statement in `scoreboard/disclosures.json`, and on or after the
    effective date, run the prescreener's three `structured=cql` results again (`mendari
    scoreboard run mendari/clinical-trial-prescreener --config structured=cql --replicates 3
    --again`, with the sandbox's HAPI FHIR up and `MENDARI_CQL_URL=http://127.0.0.1:8080/fhir`
    and `MENDARI_FHIR_ALLOWED_SERVERS=http://127.0.0.1:8080` set; each new result's accuracy
    checked against the one it supersedes, 298 of 300 on `test`), and every local result
    (`./scripts/build-scoreboard.sh`), and commit them with the page: stored pending, the CQL
    results stay "not published: disclosure pending" until then (step 8).
  - [ ] Owner: freeze the hidden sets (gate `hidden-sets-frozen`, their rotation numbers at freeze:
    decision 4, 20 runs and 180 days), mint a grading key, and on or after the effective date
    grade each pack's declared `local` configuration through the route and `mendari scoreboard
    add` each answer (step 9).
  - [ ] Owner: sign every result the branch adds (`mendari scoreboard sign`, by name), list the
    public key in `scoreboard/keys.json`, write the page again (`scripts/scoreboard_page.py`: it
    marks each unsigned result), verify, check and run the secret scan (a base64 public key may
    need one path allowed in `.gitleaks.toml`), and commit the page with the signatures (step 10).
  - [ ] Owner: publish the signed scoreboard: the docs site again and `web/` (step 11).
  - [x] Owner (decision 1): which parts, if any, are built in a private repository (default:
    none; only the key, the hidden sets' content and the budgets stay out of the repository):
    none (owner, 2026-11-07).
  - [x] Owner (decision 2): remote providers on hidden sets (default: local only under the method
    1.0, so nothing is ranked on a hidden set until this changes): local only (owner,
    2026-11-07).
  - [x] Owner (decisions 4 and 10, for 17l): the submission budget per organization, lineage and
    rotation, and who a submitting developer is and through which route (LP-19, LP-20).
    Decision 10 is confirmed (owner, 2026-11-07): 17l waits for LP-19 and LP-20. Decision 4 is
    decided (owner, 2026-11-07): 3 submissions per organization and lineage per rotation, the
    confirmation once per version; who submits stays with LP-19 and LP-20.

- [x] **LP-19** Registry accounts and deployment. **Built, 19a to 19k (2026-11-09 to
  2026-11-15); ADR-0015 Accepted by the owner on 2026-11-15 ("Accept all defaults"); left to
  the owner: the steps listed under 19k.**
  **Stopped before any code (2026-10-09; recorded
  by 18h, 2026-10-17) until the owner chose where the registry runs: A, mounted under the
  engine, on 2026-11-07** (the build session's recommendation, confirmed in chat: "the patent
  gate is clear and I approve everything on my part"). The build prompt said to stop ("Owner
  decision needed: mount the registry under the engine app (one origin, one session, one
  database) or deploy it as a separate project. If it is not recorded in tasks/todo.md, stop and
  ask."). It was not recorded, and it decides how a publisher token belongs to a signed-in user
  and organization (requirement 1), where namespace claims and their approvals live (2, 3), and
  what the deployment configuration is (4), so no part was built on a guess. **Planned
  2026-11-09: `notes/lp19-plan.md`, eleven PRs (19a to 19k), and ADR-0015 (Proposed).** Build
  PRs run against a local registry; no deploy. Conflicts between the build prompt and the code,
  written before building as the prompt asks (the plan gives each one's default): a registry
  with no sign-in still needs tokens (the operator's commands mint them, with a reason); one
  approval, at the first publish, not two; "last used" is a day kept outside the chain, since
  the chain refuses a row that changes without an event; a reviewer role must count in one
  named organization only, or every organization could approve itself; under the mount an
  archive is limited to the engine's request body limit (larger ones are LP-20's); the
  function timeout is already 300 s; the shipped registry key file holds keys only and ships
  empty until the owner makes the key; the admin token goes entirely, last. Ten owner
  decisions are open in the plan, each with a default the PRs follow until told otherwise.
  - [x] **19a** The plan and ADR-0015 (Proposed).
  - [x] **19b** Owned publisher tokens (store and operator commands): `hub_publisher_tokens`,
    `hub_token_use`, `mendari registry token|tokens|revoke-token`; tests in
    `tests/test_registry_tokens.py`; threat rows S22 and R6 (first halves).
  - [x] **19h** The client refuses a registry that is not https, except on this machine
    (`core/https_only.py`; tests in `tests/test_hub_client.py`).
  - [x] **19c** Namespace claims, reserved names, a first publish invisible until approved
    (store and operator commands): `hub_claims`, `RegistryStore.claim|decide_claim|claims`, one
    `Viewer` rule on every read, `mendari registry claim|approve|refuse|claims`; tests in
    `tests/test_registry_claims.py`; threat rows S23, I19 and R6 (second half).
  - [x] **19d** The registry mounted under the engine (`MENDARI_REGISTRY`, off by default) and
    people's routes for claims and tokens (`server/registry_routes.py`, `/api/registry/...`,
    the `keys` permission, people only); the mounted registry reads no cookie and no
    administrator token; tests in `tests/test_registry_mount.py`; threat rows S22 (second
    half), I19 and D15. Turning it on anywhere is the owner's step.
  - [x] **19i** The registry counter-signs what it accepts and the client checks it
    (`hub/acceptance.py`, `hub_acceptances`, `checked.accepted`, `mendari registry countersign`,
    `hub/registry_keys.json` shipped empty); tests in `tests/test_registry_acceptance.py`;
    threat row T11. Followed the default of owner decision 6 (a version with no
    counter-signature on a registry that has a key is refused) and of conflict 7 (the shipped
    file holds keys only). Left to the owner: make the key, send its public half for the
    one-line change that ships it, and run `mendari registry countersign` once.
    The owner took the recommended answer to each of the PR's five questions (2026-11-10):
    (1) a machine can require the counter-signature (`MENDARI_REGISTRY_COUNTERSIGNATURE`,
    `required` or `shipped`; built in 19i, off unless set; make `shipped` the documented
    setting for the hosted registry when its key ships); (2) a server stops automating a
    cached copy that was never counter-signed once its registry has a key, checked where it
    already checks recalls (a PR of its own, after 19d); (3) no expiry on records and no signed
    "latest" for launch: recalls stop a version, and freshness is taken up with the signed head;
    (4) a signed daily head of the registry's chain, as the engine's checkpoint is, before the
    hosted registry gets its key (a PR of its own); (5) no key rotation for launch: the key is treated as
    permanent, and `docs/operations/registry.md` says what a rotation still needs (a chained
    command that signs the existing versions again).
    - [x] Answer (2) built (2026-11-11): `server/recalls.py` reads `GET /v1/registry-key` in
      the same read as the recalls and holds a copy whose marker is not `accepted: true` once
      the registry has a key ("pull it again"); tests in `tests/test_registry_recalls.py`
      (SQLite and Postgres); T11's residual and the registry guide's known limits say the gap
      is closed for servers (`Decision.from_hub` and `mendari decide` still do not check).
  - [x] **A signed head for the registry's chain** (owner decision 4 of 19i, 2026-11-10,
    brought forward from "not in LP-19"): `hub/checkpoint.py` (`mendari.registry-checkpoint/1`,
    signed with the registry's key), `mendari registry checkpoint` (read-only, prints the
    statement, `--previous` for the daily routine), `mendari registry verify --checkpoint`,
    `mendari registry countersign --checkpoint` (says when it ran without one); nothing stored
    in the database, so no new table; tests in `tests/test_registry_checkpoint.py`; threat rows
    T7 and T11. Left to the owner: where the statements are kept and what runs the command
    daily for the hosted registry (the PR's questions), before it gets its key.
  - [x] **19e** Registry reviewers (2026-11-12): `MENDARI_REGISTRY_REVIEW_ORG`, the reserved
    role `registry_reviewer` and the people-only permission `registry_review`, which the role
    grants only for a membership of that one organization (given in any other: `422`; held
    there anyway: nothing); the queue and the acts that were the administrator token's at
    `/api/registry/review/...` (`server/registry_routes.py`), each chained by the reviewer's
    id; the operator's `mendari registry publisher-type|review|recall|delete`; tests in
    `tests/test_registry_review.py`; threat rows E17 and R6 (reviews). Off without the
    setting, the mount, memberships mode and an organization with a record (the server says
    why and starts). The administrator token of a registry of its own is unchanged until 19g.
    As built, where the plan left room: the review organization's record is checked when the
    server starts, so the operator makes it first (`mendari access org create`); the queue is
    the 50 oldest pending claims with each namespace's 5 newest versions, the rest counted;
    `/api/auth/me` tells a person acting for the review organization so
    (`registry_review_org`), and the dashboard's role picker offers the role only there (the
    queue's own page is 19f); `decide_claim` writes its two event names out. One adversarial
    review: no way found for anyone but a reviewer to act or read the queue; its fixes are in
    (a reviewer acts on nothing of a namespace they claimed; the queue carries no card, and
    one version is read with its card at `GET /api/registry/review/packs/...`; a malformed
    signing key is a `503` on a recall; a note or reason of spaces is refused; an unchanged
    publisher type chains nothing; a reviewer's writes are rate-limited per person; the server
    logs how many members hold the role). Not changed: a malformed
    `MENDARI_REGISTRY_REVIEW_ORG` stops the server as any malformed setting does. Left to the owner
    (decision 2): which organization reviews, and setting it anywhere. Questions for the
    owner, each built at its default: (1) a reviewer may act on a namespace claimed by a
    colleague of another organization they belong to, only not one they claimed themselves
    (the plan's rule, which the review widened from the decision to every act); (2) the
    review organization's administrators choose the reviewers, and may give themselves the
    role; (3) a review's `reviewer`, served with the version, is now a person's id
    (`user:<hash>`), not `admin`.
  - [x] **19f** The dashboard (2026-11-13): `dashboard/registry.js` adds **Namespaces** and
    **Publisher tokens** to the Access view (`extendAdmin` in `admin.js`), for an
    administrator who signed in where `/api/health` says `registry`: claim (a reserved or taken
    name refused in words), each claim's status in words with the reviewer's note when
    refused, add a publisher key; mint (the token in the one-time dialog only), revoke after a
    confirmation, who made each token, its expiry and last day used. And the **Registry
    review** screen (`#registry-review`) for a person `/api/auth/me` marks
    `registry_review_org` who holds `registry_review`: the pending claims, each version's
    `checked` beside its `claimed`, the rendered card read as text when opened, Approve
    (offered only once a version is published) and Refuse with a one-line note; a claim the
    reviewer filed shows without either. Someone holding nothing but `registry_review` sees
    that screen alone (`state.reviewOnly`, as a labeller alone sees labelling) and is not
    asked the notice (reviewing is not gated). Styles from tokens; no route, rule or pack
    changes. Tests: `tests/browser/test_registry_accounts.py` (the plan's six, plus a
    reviewer's own claim and axe on every screen in both themes). As built, where the plan
    left room: the reviewer's other acts (publisher type, a version's review, recall,
    deletion) stay API and command-line only; the screen decides claims, which is what a
    first publish waits on. Someone holding `label` and `registry_review` and nothing else sees
    both screens (each where it is served), as one holding either alone sees theirs.
  - [x] **19j** Deployment configuration (2026-11-14), configuration only, nothing turned on:
    `hub/vercel.json` (the Hub's build, an ignored-build step for commits that leave `hub/`
    alone, no host, project or variable); `vercel.json` unchanged (300 s already outlast the
    regression check's 180 s); `server/vercel.py` lists the registry's five settings;
    `deploy/vercel/README.md` "Registry and Hub" (the order to turn things on, the firewall kept
    off `/registry`, what to watch, how to turn it off); the on-premises `.env.example` and the
    Kubernetes template carry the settings commented out; `deploy/status/checks.yaml` and
    `docs/operations/status-checks.md`: the registry's base under the engine is
    `{api}/registry`; `docs/operations/registry.md` "The hosted shape" and its runbook lines.
    Tests: `tests/test_vercel.py` (the deadline, the bundle, the Hub's project file) and
    `tests/test_status_checks.py::test_the_registry_check_reads_the_mounted_registry`. As built,
    where the plan left room: the on-premises setting is in `.env.example`, which the compose
    file reads, not in `docker-compose.yml`. Left to the owner (each in the Vercel guide's
    order): the review organization's record, `MENDARI_REGISTRY_SIGNING_KEY`, the switch and
    `MENDARI_REGISTRY_REVIEW_ORG`, a reviewer, the Hub's project with its `REGISTRY_URL` and
    domain, the status checks, and the daily checkpoint's routine.
  - [x] **19g** The shared admin token removed (2026-11-14; breaking for a self-hosted
    registry, on the owner's "Start 19g"). `create_registry_app` takes no administrator token;
    `MENDARI_REGISTRY_ADMIN_TOKEN` is read only to log once that it is unused and what replaces
    it. Gone: `POST /v1/namespaces`, `PATCH /v1/namespaces/{namespace}`,
    `POST /v1/namespaces/{namespace}/tokens`, `POST …/review`, `POST …/delete`, the
    administrator's recall, and the admin branches of the publisher check, the write bucket,
    the actor, the reader and the report route. A `hub_tokens` row no longer authenticates; its
    row and event stay. `RegistryStore.issue_token` and `token_namespace` are removed; the
    store's default actor is `operator`. `scripts/hub_seed.py` takes `--db` and claims, mints,
    approves, sets the publisher type and reviews on the database; CI's `hub-site` and `hub-e2e`
    drop the variable; the docker sandbox mounts the seed script into the registry container
    (checked against a fresh image). Tests: the `registry` fixture and the live `hub` fixture
    build their namespace and token through the store (`Registry.operator`, `Hub.operator`);
    every test that used the token gets its write from the store, with no assertion about what
    a version shows changed; new `tests/test_registry_tokens.py::
    test_no_shared_secret_publishes_creates_reviews_or_deletes` and
    `::test_a_token_from_before_owned_tokens_is_refused_and_the_chain_still_verifies`, and the
    database-from-before test now builds such a database itself. As built, where the plan left
    room: `test_writes_are_limited_per_token_before_their_body_is_read` became
    `..._per_namespace_...` (the admin's bucket was the per-token case; the gate's probe
    follows); the registry guide gains "What the operator does" (replacing "What an
    administrator does") and "From a registry with an administrator token".
  - [x] **19k** Close-out (2026-11-15). Gate `a10-registry-hub`'s LP-19 pending line replaced by
    the acceptance tests of 19b to 19j (owned tokens, claims, the mount, reviewers, the browser
    flow, no shared secret, https, the counter-signature, the deployment configuration) and an
    owner line: the registry turned on and the Hub deployed (a build prerequisite carries no
    attestation, so it is a `pending: "owner: ..."` line a build PR removes once they are).
    `status-page`'s pending line names the owner's steps alone and takes 19j's status-check
    test; LP-17l's line names LP-20 alone; `a11-site-network`'s marks LP-19 done. ADR-0012's
    status and defaults 3 and 5 amended; ADR-0015 Accepted (the owner, in chat, 2026-11-15:
    "Accept all defaults", the ten defaults of `notes/lp19-plan.md`). Threat model: D15's line about
    the administrator's routes dropped, the chain's residual names a reviewer's or the
    operator's notes, and a residual for names that stay taken and tokens that outlive their
    maker. `docs/operations/revoking-access.md` lists publisher tokens; the authoring guide
    shows claiming, minting and publishing. **Owner steps left** (each in
    `deploy/vercel/README.md`, "Registry and Hub", in order): the review organization's record;
    `MENDARI_REGISTRY_SIGNING_KEY` for Production (and its public half shipped in
    `registry_keys.json` by a one-line PR); `MENDARI_REGISTRY=on` and
    `MENDARI_REGISTRY_REVIEW_ORG`; a reviewer; claiming `mendari` and `openhealth` and
    publishing the reference packs with an owned token, then `mendari registry countersign`;
    the Hub's project, `REGISTRY_URL` and DNS; the status checks; the daily checkpoint.
    **Done by the owner, 2026-11-15** (reported in chat): the review organization, the signing
    key, the switch and redeploy (health, key and `/api/health` checked), the owner as reviewer,
    `mendari` claimed, approved and verified with the five reference packs published and
    counter-signed, `openhealth` claimed (pending, holding the name), the publishing token
    revoked, the first checkpoint kept outside the database; the key shipped in
    `registry_keys.json`. Left: the Hub's project and DNS, the status checks (both at launch,
    behind Vercel Authentication until then), and the daily checkpoint as a routine.
  The options were:
  - **A, mount the registry under the engine (the build session's recommendation).** The
    registry's routes under the engine app (`/registry/v1/...`, or its own host on the same
    project): one origin, the engine's sign-in and memberships (LP-12) for people, one Postgres
    with the `hub_` tables beside the engine's, one set of headers, limits and alerts, one
    backup and runbook. Publisher tokens become credentials an organization's member mints and
    revokes in the Access view (owner, created, last used, expiry), claims and their approval a
    reviewer's queue there; the shared admin token goes. The Hub (Next.js, `hub/`) is a second
    Vercel project that reads the registry's public routes. Cost: the registry's traffic and its
    regression checks (a child process per publish, up to 180 s) share the engine's functions
    and database; on Vercel a publish's check needs a longer function timeout than a decide.
  - **B, a separate project** (`registry.<domain>`): its own Vercel project and Neon database,
    sign-in through the same identity provider but its own sessions, organizations mirrored or
    looked up from the engine, its own runbook and backups. Isolates load and failure, at the
    cost of two account systems to keep in step.
  Either way, after the choice (Claude builds, the owner deploys): tokens owned and expiring,
  with self-service create, list and revoke; namespace claims (`mendari/` and `openhealth/`
  reserved), a first publish invisible to search, pull and the Hub until a reviewer approves
  it; an organization linked to its namespaces; the deployment configuration (configuration
  only); the registry's key counter-signing accepted versions, shipped with the client; https
  required but on localhost. **Owner steps then** (A chosen, so no project or database of its
  own): set `MENDARI_REGISTRY_SIGNING_KEY` (32 random bytes, base64) for the registry's own
  recalls and counter-signatures; DNS for the registry and the Hub; the Hub's `REGISTRY_URL`.
  LP-22 (the site evaluation network) needed LP-19's organizations in the registry; LP-19 is
  built, so it no longer waits on it.

- [ ] **LP-20** The pack store (A7; gate G3-4's `a7-pack-store`). **Planned 2026-11-10:
  `notes/lp20-plan.md`, eleven PRs (20a to 20k), and ADR-0016 (Proposed).** Each organization
  gets a store of pack versions in the database, keyed by content digest: authors
  (`pack_author`) upload a signed archive or copy a built-in pack, edit, validate and test a
  draft; evaluations are stored jobs with a ledger, a draft compared with the current version
  on the same cases; promotion is its own chained, digest-bound record at every risk level,
  signed off by someone other than the author. Off unless `MENDARI_PACK_STORE=on`, memberships
  mode only; no reference pack is rebuilt and no valid pack decides differently. Conflicts
  between the build prompt and the code, written before building as the prompt asks (the plan
  gives each one's default): the registry never ran the validator, so "one validation function"
  makes it refuse what it takes today; "too few to estimate" means the dataset command fails
  only on schema and label problems; a name list would trip on synthetic names, so a name
  counts only beside another identifier; archives larger than the 4 MiB body limit are uploaded
  in parts and kept in the database (32 MiB at most), with no object storage; a copy runs on
  the bounded engine, so the trial and referral packs cannot be copied until 16c-2 rewrites
  their lookaheads; a store pack's namespace is its organization's id (or a namespace it holds
  by an LP-19c claim), and a copy is renamed; an upload is signed by an author key the
  organization registered; drafts never change, so an edit is a new draft, text files only,
  and hosted training is not built; with no hidden-case index key the hidden-set check is "not
  applicable"; a stored job advances in slices a caller asks for, since there is no worker; a
  promoted version is built on first use in a process, not at start-up (LP-16d's rule is for
  packs on disk); proposed tests from reviewers' corrections stay beside the draft, sealed,
  never in a pack's files, because they are patients' inputs; promotion is the rule for store
  versions only, and packs on disk are served as today. Fifteen owner decisions are open in the
  plan, each with a default the PRs follow until told otherwise. Threat-model rows reserved:
  S24, S25, T12, T13, R7, I21, I22, I23, D16, D17, E18, E19.
  - [x] **20a** The plan and ADR-0016 (Proposed).
  - [x] **20b** One validation function (`mendari.evals.validation`: `problems` and `validate`,
    each problem a kind, a file and a place, never a case's text), called by the command line,
    the community check and the registry's publish, which now refuses what the validator
    refuses (decision 1's default). A behaviour test's input problem is worded as a decision's
    reasons are. Validating a reference pack takes under 0.25 s (open point 6).
  - [ ] **20c** dataset checks and `mendari data check`. **20d** the PHI detector.
  - [ ] **20e** The store as a library: tables, versions that never change, the origin `store`
    on the bounded engine, the prerequisites test, the operator's commands. No route.
  - [ ] **20f** The author role, and the upload, copy and edit routes with the PHI and
    hidden-set checks (`MENDARI_PACK_STORE`, off by default). Adversarial security review.
    LP-17l can start once this is merged.
  - [ ] **20g** Evaluation jobs with a ledger, dataset upload, and draft against current.
    **20i** the improvement dataset and proposed tests, in parallel.
  - [ ] **20h** Promotion, and serving the promoted version to its organization only.
    Adversarial security review; it changes which pack answers a request, so it needs the
    owner's yes before it merges.
  - [ ] **20j** The dashboard: upload, results, comparison, promotion; the browser test in a
    second organization.
  - [ ] **20k** Close-out: gate `a7-pack-store`, the threat model, ADR-0016 Accepted on the
    owner's word.
  Owner decisions open, each with its default (`notes/lp20-plan.md`, "Owner decisions still
  open"): (1) the registry refuses a publish the validator refuses: yes; (2) the store needs
  memberships mode: yes; (3) the name list's source: common given names and surnames from
  United States government publications, in the licence inventory; (4) who may author: only
  holders of `pack_author`; (5) who may promote: `governance` and `admin`, never the author;
  (6) archives: 32 MiB, in the database, in parts; (7) 16c-2 is not part of LP-20; (8) author
  keys are registered by an administrator; (9) jobs advance in slices, no cron route;
  (10) promotion is not required of packs on disk; (11) a one-person organization cannot
  promote, with no way round; (12) 3 held uploads in 30 days stop an organization's uploads, 20
  uploads a day; (13) remote providers on drafts only with the organization's evaluation quota
  set, 200 cases a job; (14) 256 MiB of files an organization; (15) every store pack is bounded.
  **Owner steps then:** confirm the name list's licence before 20d merges; set
  `MENDARI_PACK_STORE=on` after 20f to 20h are merged and reviewed; give `pack_author` and make
  sure each organization has a second person who may promote; register author keys; run
  `mendari store recheck` once hidden-set keys are set; read `mendari store held` when a
  version is held; give the external reviewers the store's scope (gate `external-review`).

- [ ] **LP-21** Operations for a public service (ADR-0010, Accepted 2026-11-07). Planned by a
  mapping workflow (2026-10-08) as fifteen PRs, each safe alone; none changes what a valid pack
  decides, and anonymous `/api/health` stays three fields with no database work:
  - [x] **21a** (#194, merged 2026-10-08) Readiness, `GET`/`HEAD /api/health/ready`: one read of the chain's
    table (`AuditStore.ping`, creating nothing) per instance every 5 s, whoever asks, in a
    thread of its own waited for about 3.5 s, a failure kept too; a read never answered holds
    that one thread and none starts again until it returns; a caller during a check gets the
    last answer at once; the packs as start-up found them (`ok`, `degraded`, `none`); `503`
    when unready; fixed codes only. Kubernetes' readinessProbe uses it; ADR-0010; this entry.
  - [x] **21b** (#197, merged 2026-10-08) The status checks file (`deploy/status/checks.yaml`: readiness, the
    sign-in redirect, a synthetic decide on AI Guard (local rules, even with Jev set up) by a
    decide-only key in a status organization of its own, the registry, the Hub),
    `scripts/status_checks.py`, `docs/operations/status-checks.md` and `/status`, a public
    static page that reads readiness in the browser.
  - [x] **21c** (#195, merged 2026-10-08) The monitor compares like with like: the served version (others
    counted apart) since its current configuration started; the benchmark only for this build,
    these providers and models, and this governance (`benchmark.reason` otherwise:
    `no_benchmark`, `another_build`, `providers_unknown`, `other_providers`, `other_policy`);
    accuracy stratum by stratum (reviewed, automated), alerting only beyond doubt; refusals
    apart; drift over 50 confidences; whole weeks, 14 to 180 days; awaiting review counted; an
    unknown cost unknown. The CLI checks each pack's newest version. The view, README, guide and
    spec say so.
  - [x] **21d** (#198, merged 2026-10-08) Operational health in the monitor: provider error share (an additive
    `decisions.provider_errors` count, written beside the record; NULL on older rows, left
    out), abstention share and latency (p50, p95, mean), with week-over-week alerts
    (`provider_errors`, `abstention`, `latency`) over 50 decisions or more, needing no
    benchmark; the view's weekly table shows them.
  - [x] **21e** (#196, merged 2026-10-08) Alert delivery (`mendari.alerts`): `MENDARI_ALERT_FORMAT` (`mendari`
    JSON or `slack`), an HMAC signature (`MENDARI_ALERT_SECRET`: `Mendari-Signature`, with the
    receiver's recipe in the guide, run by the tests), retries (no answer, `408`, `429`, `5xx`)
    and an `alert_deliveries` record (never the URL or the secret); the quota alert one try on
    the decision, recorded for `retry_due`; `mendari monitor` exits 3 when its alerts could not
    be posted; threat model S17.
  - [x] **21f** (#199, merged 2026-10-08) `GET /api/internal/monitor`, guarded by `CRON_SECRET` before any
    database work (`require_cron`, shared with the purge): pending alerts sent again, then each
    organization with decisions on its own (but `MENDARI_STATUS_ORG`), one recorded alert each,
    retries for 60 s, no organization started after 180 s, an alert naming those left, a
    different first organization each day; `mendari monitor --each-org` (0 when every alert
    was posted, 1 on an error, 3 when one was not); the templates run it, the Kubernetes job
    never re-run; gate `hosted-monitoring` names its tests and waits only for the owner's alert
    webhook (the schedule, 21n, is merged).
  - [x] **21g** (#201, merged 2026-10-08) The usage ledger (`usage_ledger`: local day, organization, provider type,
    purpose `decision` or `evaluation`; calls, priced calls, tokens, the priced calls' cost),
    written with the decision (or the evaluation's report) in its transaction from each
    decision's meter (`providers.base.Meter`: `sending` counts a call as it is sent, `metered`
    its tokens and price, both bounded); `AuditStore.usage_ledger` shows a cost only when every
    call was priced.
  - [x] **21h** (#203, merged 2026-10-08) Evaluations count against their organization: `eval_jev` and `eval_llm` in
    `MENDARI_ORG_DAILY_LIMITS` (unset: no limit), charged by `CallBudget.for_evaluation`, never
    the decisions' quotas; the quota alert says what an evaluation quota stops.
  - [x] **21i** (#216, merged 2026-10-08) `GET /api/usage` (administrators: their organization's ledger over `days`, 1 to
    366, with totals by provider and purpose, a cost only where every call was priced), the
    Access view's Usage tab, and `mendari usage --days --org` for the operator.
  - [x] **21j** (#202, merged 2026-10-08) Audit checkpoints and incremental verify: `audit_checkpoints` (seq, hash, when;
    outside the chain), `mendari audit checkpoint` (verify from the last, record the head),
    `mendari audit verify --since-checkpoint`; a full verification checks every checkpoint
    (`reason: checkpoint` for a chain rewritten from before one or cut short of it).
  - [x] **21k** (#209, merged 2026-10-08) The signed daily checkpoint: `MENDARI_CHECKPOINT_KEY` (Ed25519 seed, base64)
    signs the day's first checkpoint (`checkpoint_statements`; `{format, date, head, key_id,
    signature}`, no position or count); `GET /api/audit/checkpoint` publishes the newest with
    the public key (public, read once a minute per instance); `GET /api/internal/checkpoint`
    (`CRON_SECRET`) records and signs, and alerts `audit.broken` on a chain that does not
    verify or a day's stored statement that is not this key's (never published); a rotated
    key signs the day anew; `mendari audit verify --against <statement or the route's answer>
    --public-key <key>`. The hosted schedule runs it daily at 10:00 UTC (21n); a statement is
    signed and published once the owner sets the key.
  - [x] **21l** (#200, merged 2026-10-08) Break-glass revocation commands: `mendari access keys|clients` (read-only),
    `mendari access revoke key|client <id>` and `revoke all --org` (each still in force), and
    `mendari access end-sessions --kind people|launches|all` (never the assertion claims),
    straight to the database for any organization, each with a reason chained
    (`key.revoked`, `client.revoked`, the deployment's `sessions.ended`);
    `docs/operations/revoking-access.md`; the hold page's table names the command line.
  - [x] **21m** (#218, merged 2026-10-08) `docs/operations/incident-runbook.md` (in the nav): severities tied to the
    alerts and checks that trigger them, who is paged (the owner's), holding automation,
    revoking access, telling people, backup and restore (SQLite `.backup`, `pg_dump` /
    `pg_restore`, Neon) with a full verification against the last signed checkpoint, the
    firewall's log-only week (the owner's); `tests/test_restore.py` runs the restore on SQLite
    and Postgres in CI; gate `incident-runbook` names its tests.
  - [x] **21n** (#217, merged 2026-10-09) The hosted schedule (`vercel.json` crons): the purge
    at 08:00 UTC, the monitor at 09:00, the checkpoint at 10:00, each daily; a test pins them
    and runs each with the secret, and gate `hosted-monitoring` names it. It has run daily
    since (the production logs show all three routes answering `200` on 2026-10-15, 10-16 and
    10-17). The owner's go-ahead to merge was not recorded in writing: **the owner confirmed it
    after the fact on 2026-11-07** (in chat: "the patent gate is clear and I approve everything
    on my part"). Its alerts reach someone once the owner sets `MENDARI_ALERT_WEBHOOK` for
    Production, which `mendari alerts test` checks; the gate stays pending until then.
  - [ ] **21o** Firewall enforcement: **owner only**, after the log-only week.

  **Defaults each PR follows** (each confirmed by the owner on 2026-11-07; ADR-0010 Accepted with
  them, its owner actions below still open):
  1. (21a) Readiness says fixed codes and the version only: no pack ids, counts or error text.
  2. (21a) Its rate limit is the cache: one database read per instance every 5 s whatever the
     request rate, and one request thread at most. No per-address `429`: it would fail a probe
     sharing its address with other callers (review of 21a).
  3. (21a) `HEAD` on readiness only; `/api/health` keeps `GET`.
  4. (21a) A pack that did not load or build makes readiness `degraded` (`200`); `503` only
     when the database fails or no pack is built.
  5. (21a) Kubernetes: readinessProbe on readiness (10 s, 3 failures); liveness and the Docker
     `HEALTHCHECK` stay on `/api/health`.
  6. (21b) The in-app status view is minimal; the owner's external status page is the one that
     survives a full outage.
  7. (21c) No leaderboard fallback: a benchmark that is not comparable leaves no reference.
     The hosted site runs Jev where the reference packs' benchmarks measured `naive_bayes` and
     rules, so it gets no accuracy or drift alerts against them until a benchmark measures
     its configuration; the view and `mendari monitor` say why.
  8. (21c) A governance approval in force is part of the configuration: a benchmark measured
     without one is not compared with a pack automating under one (`other_policy`).
  9. (21h) Evaluations get their own organization quotas, unset meaning no limit; charging
     them to the live quotas could push live decisions to review (clinical behaviour).
  10. (21k) The public checkpoint carries no sequence number or count, and is published only
     once the owner sets the key.

  **Owner decisions** (none blocks 21a to 21n). Confirmed (owner, 2026-11-07): the evaluation
  quotas stay unset (`eval_jev`, `eval_llm`: no limit), and evaluations share
  `MENDARI_JEV_DAILY_LIMIT` and `MENDARI_LLM_DAILY_LIMIT` with live decisions, with no
  deployment budget of their own. Still open:
  - the status page provider, its checks and who is paged (B36). `python
    scripts/status_checks.py --export json --api <MENDARI_PUBLIC_URL>` prints the checks to set
    up. No outside check reaches the hosted site until Vercel Authentication is lifted; the
    decide check waits for a status organization (the hosted site runs memberships mode since
    2026-11-07; the `status` organization and its key are not made yet), and the registry and
    Hub checks for LP-19 (gate `status-page`'s pending line);
  - the alert format, secret and destination. Set `MENDARI_ALERT_WEBHOOK` (https),
    `MENDARI_ALERT_SECRET` (`openssl rand -hex 32`) and, for a Slack webhook,
    `MENDARI_ALERT_FORMAT=slack`, for Production only and marked sensitive; redeploy; then run
    `mendari alerts test` on your own machine with the same three values: it should say
    `delivered`, and the receiver should accept the signature. Then a build PR removes gate
    `hosted-monitoring`'s pending line;
  - the checkpoint key (`MENDARI_CHECKPOINT_KEY`) and where its public key is published;
  - firewall enforcement (21o).

- [ ] **LP-22** The site evaluation network (A11): sites share signed counts of how a pack did
  on their own chart-reviewed data. **Planned 2026-11-10: `notes/lp22-plan.md`, ten PRs (22a to
  22j), and ADR-0017 (Proposed; 0016 is left for LP-20's plan).** LP-19 is built (2026-11-15),
  so no step waits on it any more (22d needed 19c, 22f 19g, 22h 19i, 22i 19d to 19g). No step needs an LP-20 step as LP-20 is described
  today. Build PRs run against a local registry with synthetic sites; nothing is deployed, and
  no PR drafts or attests a site agreement. Conflicts between the build prompt, the notes and
  the code, written before building as the prompt asks (the plan gives each one's default):
  the signing code to reuse is `hub/archive.py`'s key helpers and a recall's record shape, not
  the archive's own signature; `mendari.site-report/1` holds rates, so a counts-only `/2`
  replaces it, and ECE, Brier, latency and cost stay with the site; subgroups stay out until
  packs declare their values; a site signs, so publisher tokens stop sharing reports and
  `--share` stops taking a free name; a registry with no sign-in registers and verifies sites
  with the operator's commands; the scoreboard page reads no registry, so the Hub shows site
  evidence and the scoreboard only links to it; the statistics are pure Python checked against
  scipy in tests; the exact test where the tables are few, a seeded permutation test
  otherwise; the random-effects method (unnamed in the prompt) is DerSimonian-Laird on the
  logit scale from 3 sites; a correction supersedes in the open rather than replacing; a
  ticket's expiry is worked out on read, since the chain refuses a row that changes without an
  event; the digest is checked, not only the id and version; consent is asked whenever a
  configuration uses a provider type outside the local set; LP-22 does not make the engine ask
  for a site report before automation (B42 stays the owner's attestation); unsigned reports
  from before are kept and no longer served. Twelve owner decisions are open in the plan, each
  with a default the PRs follow until told otherwise: site names in public; the publisher's
  own site; the suppression numbers (1 to 9, and no sharing under 100 cases); the
  random-effects method; a ticket's 30 days; who registers and who verifies; remote-arm
  evidence; which figures are pooled; a compromised key; whether automation should need a site
  report; how many figures the homogeneity test covers; whether the commitment is ever shown.
  - [x] **22a** The plan and ADR-0017 (Proposed).
  - [x] **22b** The statistics of pooling: fixed effect from summed counts, random effects, the
    range, and the homogeneity test (pure Python, checked against scipy and statsmodels;
    `evals/pooling.py`, `tests/test_pooling.py`).
    **22c** in parallel: `mendari.site-report/2` (counts only) and one suppression function.
  - [x] **22c** `mendari.site-report/2`, counted from the ledger, and `suppress` on both sides
    (`evals/share.py`, `figures.site`, `tests/test_share.py`). Beyond the plan: error counts and
    the sweep's finer rows stay with the site (each would let a suppressed cell be worked out),
    and complementary suppression also guards against bounds (no count negative, a critical
    row within its n), not only subtraction.
  - [ ] **22d** Sites and site keys in the registry's store, and the operator's commands (after
    19c).
  - [ ] **22e** Evaluation tickets, the keyed dataset commitment, `mendari site init`, and
    consent for a remote arm.
  - [ ] **22f** Signed submissions checked on receipt, one live result, `--share-out` and the
    offline path (after 19g; one adversarial security review).
  - [ ] **22g** Aggregation served per digest and configuration. **22h** the Hub's forest plot,
    apart from every rank (after 19i).
  - [ ] **22i** People register sites in the dashboard and reviewers verify them (after 19d to
    19g; beside 22e to 22h).
  - [ ] **22j** Close-out: gates `a11-site-network` and `site-validation`'s pending line, the
    threat model (rows S26, S27, T14, T15, R9, I24, I25, I26, D18, E20), ADR-0017 Accepted on
    the owner's word.
  **Owner steps** (no PR does them): the agreements with each site (gate `site-agreements`);
  checking who each site is before verifying it; telling each site where its key and
  commitment secret are kept; attesting `site-validation` per site with the signed report's id.

## Plan: an engine that learns (2026-11-10)
The owner's request, in their words: make Mendari, "the infrastructure the packs are built on",
"intelligent, robust, and able to self improve". Outside the launch plan's LP steps: it gates
nothing there. Built on one branch (`claude/mendari-robustness-self-improve-fc257e`), one commit
a step; **merging is the owner's decision**, since step 1 changes what a pack decides on some
inputs.

### Objective
An engine that reads what people actually type, learns from what its reviewers decide, and keeps
what it learns only when that is measurably better and no less safe. It proposes; a person
promotes: nothing changes a pack, or holds or lifts automation, by itself (ADR-0010, and
`evals/feedback.py`'s "improvements stay reviewed and versioned").

### What the examination measured (2026-11-10, default offline providers, the packs' own data)
- **Typography hides text from every pack's patterns.** A phone's curly apostrophe: P(urgent) of
  "it won't stop bleeding" falls from 0.98 to 0.12 (3 of the 55 emergencies the inbox rules
  flag). Every third space no-break and every third doubled: the inbox screen keeps 32 of its
  55, the referral screen 108 of its 136, the prior-authorization screen 13 of its 35. An en
  dash in "every 4 to 6 hours": the AI Guard's dose check calls 6,000 mg of acetaminophen a day
  within the limit, at 0.98. No case was automated by it (the default configurations automate
  nothing without a model beside the rules).
- **The engine never learns from its reviewers.** A resolved review stores the label a person
  chose (`decisions.final_label`), and nothing exports it: `mendari feedback export` reads
  thumbs only.
- **Learning from new labelled cases works.** The inbox router, on the harder draft's held-out
  split (436 cases, sharing no family or paraphrase with what it learned from): 73.9% as shipped,
  81.2% with 332 new labelled cases added to its training data; on the local-site example 93.2%
  to 98.5%; on the pack's own test 99.3% to 99.2%.
- **Character features help the built-in learner.** Word and character features, same data:
  81.0% as shipped, 85.1% with the new cases, and 6 confident mistakes (at 0.90 or more) where
  the shipped model makes 12.
- **Tried and not built.** A logistic-regression learner: 100% on the pack's test, but 49
  confident mistakes on the held-out split where naive Bayes makes 12 (overconfident off its
  training text). A cap on automation when few of a message's words are known to the model: it
  held back most of the held-out split but did not tell right from wrong (on the draft's train
  split, 21 of the 67 "familiar" confident answers were wrong).

### Plan
- [x] **Step 1: text is read in its plain form** (ADR-0018). `core/text.py` `plain`; `rules`,
  `dosing` and `naive_bayes` read through it (`providers.base.text_from`), `criteria` a
  medication's name. Typography only, never longer than the text, its own plain form. Tests
  (`tests/test_text.py`): each mapping, the two properties (hypothesis), every true critical
  case of every reference pack retyped six ways deciding as shipped, what the same retypings
  hide when text is matched as received (15 figures), no text of any pack's datasets changed by
  it (so data, models, benchmarks and digests are byte-identical), the spec's list of
  characters equal to the code's. Passes on Python 3.11, 3.12 and 3.13.
- [x] **Step 2: reviewed decisions as labelled cases.** `AuditStore.labelled` (a pack's
  decisions with a reviewer's label or a recorded outcome, in the caller's organization) and
  `evals/feedback.py` `reviewed_dataset`; `mendari feedback export --source reviews [--limit N]
  [--org ORG]` and the same route with `?source=reviews` (the existing admin route, so its
  permission, rate limit and log lines are the ratings export's). The label is `truth_label`'s:
  the outcome when one was recorded, else the reviewer's. No person is named, no note is
  carried, a hashed or sealed input is counted and left out, and a sealed one opened for a
  reader is chained. Tests (`tests/test_reviewed_cases.py`), SQLite and Postgres, the route
  with sign-in on too. Not built: a dashboard button for it, and `review` / `outcome` among the
  blocks a hidden set refuses (the `system` block every exported record carries already
  refuses it).
- [x] **Step 3: character features for the built-in learner**, off unless a model file says
  so. `providers/naive_bayes.py`: `tokenize(text, character_ngrams)`, a model's
  `character_ngrams` and its own format (`mendari.naive_bayes/2`), checked at load both ways (a
  /2 file must hold the key, a /1 file must not). `mendari train --character-ngrams LOW-HIGH |
  none`; with neither, each model is rebuilt as its file says (`evals/training.py`
  `built_with`), so the reference models rebuild byte for byte. The pack format and the
  provider's config are unchanged: the option lives in the model file, which already records
  how it was trained. Tests (`tests/test_character_features.py`), one pinning the measured
  gain (322 and 353 of 436). Considered and not done: a `character_ngrams` key on the variant
  in `pack.yaml`, which would be a spec 0.3 key with its own gating and is the owner's call.
- [x] **Step 4: `mendari improve`** (ADR-0019). `evals/improve.py`: the cases a run can use
  (labelled for the question, accepted by the schema, not in the pack's data, one label a
  case), a case being what the classifier reads of it; split by a hash of that (30% held out,
  15% calibration); two candidates (the model's features, and the other set); each judged
  against the model in the pack on every declared dataset but the two it trains on and on the
  held-out cases, with the eval gate, a paired test on every dataset (exact McNemar: a loss
  beyond chance is a regression, a held-out gain beyond chance is "better"), the share and the
  count of confident answers that are wrong, and the behaviour tests. `apply` (`--write`)
  writes the model and adds the learned and calibration cases to the pack's own datasets, all
  three files or none, once, and only into a pack of one's own. Tests
  (`tests/test_improve.py`): the measured gain pinned (102 to 138 of 145 held out; 322 to 354
  of the draft's other split), nothing written unless asked, the report holds no case, a pack
  it wrote to is rebuilt byte for byte by `mendari train`, a second run learns nothing new,
  wrong labels and the pack's own data are not learned.
  **Found while building it:** the 3% the eval gate tolerates let a file of wrong labels
  through (6 of 600 test cases lost), which is why the paired test is there; and the local-site
  example's own candidate was confidently wrong on 4 emergency messages, which is why the
  confident-and-wrong check is.
- [x] **Step 5: review and the whole gate.** One independent review of the branch: 1 high, 6
  medium, 10 low; all fixed but two lows left as documented behaviour (accents composed for a
  whole field or not at all; what a run measures on a deployment with a model screen).
  - **High.** A case was its whole input, so the same message with another time stamp was
    another case, fell on both sides of the split and was judged after it was learned (given
    three times each, every held-out case came out right), and a message of the pack's own test
    was learned from once its record differed in one field. A case is now what the classifier
    reads of it.
  - **Medium.** The plain form lost detections the raw text had (a vertical tab, a form feed or
    a line separator read as a newline; a zero-width space joining the two words it separated;
    an ellipsis between two doses read as a decimal point): fixed, and the three cases that
    remain are in ADR-0018. `apply` was not all or nothing, and wrote through a path spelt
    another way into a bundled or pulled pack (`origin_of` now tells a directory by the file it
    is: before, a pulled pack reached through another spelling of its path was taken for a local
    one, which this branch did not introduce). A report-only run wrote to a model file named by
    an absolute path. The command-line export had no organization scope. `mendari train` failed
    on a pack with two classifiers after a run was kept.
  - **And a mistake of the session's own:** a test applied a run to
    `packs/patient-inbox-router` itself (restored at once; `tasks/lessons.md`). `apply` now
    refuses the bundled packs, and the suite fails a run that leaves a pack of the repository
    changed (`tests/conftest.py`).
  - Verified: lint, format and types; the full suite on SQLite and Postgres; the reference
    packs rebuilt from source byte-identical (data, models, ledgers; all five benchmarks
    reproduce); the new test files on Python 3.11, 3.12 and 3.13; the docs site (strict); the
    README's command tour and the API examples.
- [x] **Step 6: a pack that holds learned cases is not published** (2026-11-11). Every case a
  run adds says so in its record (`attributes.added_by: "mendari improve"`), written where the
  case is made: a model records the cases it was built from whole, so a mark added only when
  the files are written would stop `mendari train` rebuilding the model byte for byte. `apply`
  writes no case that does not carry it, and a model trained on such a case says so in its
  `training` record (`evals/training.py` `fit`), since it holds their words. The mark and its
  one reader are `core/site_cases.py` (`core/learned.py` until step 7). `hub/archive.py`
  `build` searches every file the archive would hold for that mark, as bytes, and refuses
  before anything is signed (`refuse_site_cases`), so `mendari pack build` and `mendari
  push` both stop there with one line naming the files (the first five) with how many times
  each is marked, never a case. The registry's publish does the same on the files it is sent
  (a client from before this step, or anything else that can sign, need not have looked) and
  keeps nothing. `mendari community-check` fails a community pack that holds the mark in any
  file, a hidden one too (rule `phi`). A case is recognised by the mark alone, not by its
  `learned-` id, which is a naming habit. `mendari improve` reads no `--with` file from the
  pack's own folder, however the path is spelt: that file need carry no mark (an export's has
  its own since step 7; a site's own file has none), and every file there goes into the
  archive. Tests (`tests/test_site_cases.py`, one in
  `tests/test_community_check.py`): both commands and the client, with nothing signed and
  nothing sent; cases taken out and the model left, refused until `mendari train` gives back
  the model the pack came with; a case in a dataset the pack only tests with, in a copy the
  manifest does not name, as one indented or one compact JSON document, in another folder, and
  cut short; a pack whose own cases only look like a run's is built as before; no pack of the
  repository holds one; the registry refuses one, after the signature, and keeps nothing.
  - One independent review of the step: 1 high, 2 medium, 4 low, all fixed here but the part
    of the high one that is the owner's (below). **High:** the `--with` file kept in the pack's
    folder was archived unmarked, held-out cases included, once the refusal's own advice had
    been followed (now `improve` reads none from there). **Medium:** taking the cases out
    passed with the model left as it was (the model is marked now); the community check
    passed a pack holding marked cases (it fails now). **Low:** the refusal named five files
    and did not count the rest, and a file name with a newline made it two lines; three broken
    guards passed the tests (only `.jsonl`, only `data/`, only one spelling of the mark); the
    count built a list of every match; the docs said "each file".
  - Verified: lint, format and types; the new and affected test files on Python 3.11, 3.12 and
    3.13; the repository-wide checks; the archives of the five reference packs byte-identical
    to the ones the code before this step builds, with one key; the full suite once.
  **Not recognised, and said so in the README and ADR-0019:** a site's cases put into a pack by
  hand (an export saved in the pack's folder and never given to a run, which step 7 closes; a
  copy compressed or in another encoding), and a pack a run was kept in before this step,
  which has no mark (this branch only, never released: improve a fresh copy). A file that only
  quotes the mark is refused like one that holds a case.
- [x] **Step 7: an export says what it is too** (2026-11-11, the owner's decision of that day:
  "Yes, mark exports"). Every record `mendari feedback export` and its route write, of ratings
  and of reviews, carries `attributes.exported_by: "mendari feedback export"`
  (`evals/feedback.py` `_attributes`), and the command says so when it writes one. Step 6's
  reader looks for both marks (`core/site_cases.py`, as `core/learned.py` is now named, since
  an exported case is not a learned one; `hub/archive.py` `refuse_site_cases`), so an export
  saved in a pack's folder, or its records copied into the pack's datasets, keeps that pack
  out of an archive, of a registry and of the community packs. A model carries the marks of
  the cases it was built from, in one order whichever case comes first
  (`site_cases.carried`), so one trained on exported cases put into a pack by hand is refused
  until `mendari train` builds it again without them. `apply` still takes only a run's own
  mark, and a run's report carries neither. A case a run adds from an export says a run added
  it, once: the export's mark stays on the file the run was given, so a file's count is still
  its cases. Nothing else changed: a pack that holds no such record is built byte for byte as
  before, and a model trained on none is the same bytes. Tests (`tests/test_site_cases.py`,
  as `tests/test_learned_cases.py` is now named; one each in `tests/test_reviewed_cases.py`,
  `tests/test_feedback.py` and `tests/test_community_check.py`): the command's file and the
  route's answer, of both sources; `--out` in the pack's folder, then `pack build` and `push`
  with nothing sent and no archive until the file is out; exported records in a dataset the
  pack tests with and as one JSON document, indented and compact; a model trained on them,
  and on both kinds; a run's report on a pack whose data holds them; the registry; a case of
  a run's with only the export's mark, not kept; a model calibrated on exported cases; a mark
  outside a record's `attributes`, and one with a tab or a line break beside its colon. Each
  test was seen to fail first, and nine ways of breaking the reader or a writer were each
  noticed by a test.
  - One independent review of the step: no high finding, 1 medium, 3 low, all acted on here.
    **Medium:** two more commands write a site's cases with no mark, `mendari omop dataset`
    and `mendari bulk dataset` (one record per patient, documented with `--out local.jsonl`),
    and the docs said the two marked commands were all there was. The docs now name those
    datasets among what is not recognised; whether to mark them is the owner's (below).
    **Low:** the refusal said to run `mendari train` once the cases are out, which fails on a
    pack with no classifier (now: where a model was built from them); four broken guards
    passed the tests (no notice with the ratings export, a model calibrated on marked cases
    left unmarked, a mark outside `attributes` missed, a tab beside the colon missed), each
    failing a test now; the README called exported records "ready-made test cases" a
    paragraph before saying a behaviour test is written afresh, and step 6 here said the
    `--with` file carries no mark.
  - Verified before the review's findings were fixed (they changed two messages, docstrings,
    docs and tests): the full suite once; the new and affected test files on Python 3.11 and
    3.13; the README's server commands and the API examples; the archives of the five
    reference packs byte-identical to the ones the code before step 6 builds, with one key,
    and both reference models trained again to the bytes they ship with. After: lint, format
    and types, the affected test files, the documentation blocks and the repository-wide
    checks.
  **What it costs, and what is still not recognised:** an export can no longer sit in a pack
  that is built or pushed, so a site that kept exported cases in its own pack as evaluation
  data cannot archive that pack, to a registry of its own either (the first question below).
  A confirmed miss becomes a behaviour test a person writes, with no patient's words in it
  (README). Not recognised: the datasets `mendari omop dataset` and `mendari bulk dataset`
  build from a site's records, a file of cases a site labelled itself, a case retyped into a
  pack or copied without its `attributes`, a copy compressed or in another encoding. Not done:
  the dashboard's Feedback view says nothing of it where it offers the download (the file it
  downloads carries the mark all the same), and still calls a thumbs-down with its right
  answer "a ready-made test", meaning a case to evaluate the pack on.

### Left for the owner
- **Merging.** Step 1 changes what a pack decides on text that holds such characters.
- **Whether a site may ever publish a pack that holds its cases.** It is refused everywhere now
  (steps 6 and 7: the cases a run added, an export, a model built from either), and nothing
  turns that off. A private registry inside one site may be a fair place for such a pack; a
  way to allow it (a flag, or a setting of the registry) is a decision about a site's data,
  and was not built.
- **Whether what `mendari omop dataset` and `mendari bulk dataset` write carries a mark too.**
  Each is a site's records, one per patient, and a file of them saved in a pack's folder, or
  declared as one of the pack's datasets, goes into the pack's archive unrecognised. A mark of
  their own, read as step 7 reads an export's, would keep such a pack out of an archive. It
  changes those commands' record format, so, as for the export, it is the owner's decision.
- **Whether the reference packs adopt any of it**: character features for their models, or the
  harder draft as training data, each a new pack version with its own review.
- **An automatic hold when monitoring alerts.** It only lowers automation, and ADR-0010 chose
  that nothing holds automation by itself.
- **The regulatory reading.** A pack is the same pack until a person promotes a new version, so
  each version is fixed; whether learning from a site's reviewers changes anything for the
  FDA CDS opinion (gate `fda-cds-opinion`) is counsel's to say.

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
  the PyPI trusted publisher; then 0.1.0 and the docs on GitHub Pages (`docs.yml`, LP-15c, run
  by hand; its deploy skips while the repository is private; owner step 9), and the README's
  install line switches to `pip install mendari`.

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
  `core/registry.py`; the shipped packs decide identically (7,484 decisions compared with main).
  Review fix: the namespace and duplicate rules apply where a community pack takes part only (a
  community pack's id is `community/<folder>`), so with the switch off a deployment serves
  exactly what it did (a pulled `mendari/` pack, a local copy, a path holding `packs/`); a pack
  under a local path takes the origin of where it sits.
- [x] **B2 Policy and spend.** A community pack is capped at HUMAN_REVIEW in `core/policy.py`
  (recommend only; ADR-0002: policy only lowers automation). Unscoped provider settings skip
  community packs; only `community/<name>:slot=variant` applies. A community pack's pinned
  `model` and `max_tokens` give way to the deployment's. Refused at load: risk level above 1,
  `store_input` other than `hash`, a default variant that is not local (`rules`, `criteria`,
  `dosing`, `naive_bayes`, or a composite of those).
- [x] **B3 Labels everywhere.** "Community · not clinically reviewed" by origin on `/api/packs`,
  `/api/packs/{id}`, `/api/health` (packs by origin), the dashboard picker and card header, Try
  it, the Decision Card, CDS Hooks discovery and each card's `source.label`. The dashboard's
  default pack is the first maintained one. `publisher.type` shown as self-declared. Tokens only;
  contrast tests extended.
  Built: `ORIGIN_LABELS` and `Pack.shown_origin`/`origin_label` (`core/origin.py`, a
  `community/` id is community wherever it was loaded); `origin` and `origin_label` on the pack
  endpoints and the SMART session's packs, `packs_by_origin` in `/api/health`; the Decision
  Card's Source row (maintained and community only) and "Automation: Never" for a community
  pack (the five shipped cards regenerated: two lines each, digests unchanged); CDS Hooks
  discovery and `source.label` for community services only; the `/docs` landing labels them
  (open mode, as before); the picker groups by origin when more than one is served; the overview
  header, the card, Try it and the approval section. Tests in `tests/test_community_packs.py`
  and `tests/browser/test_community.py` (both themes).
- [x] **B4 `mendari community-check`** (and `scripts/community_check.py`, shared by the CLI and
  CI): scope, identity, manifest policy (`license: Apache-2.0`, `publisher.type: community`,
  `store_input: hash`, local providers, no pinned model, `degradation_tolerance` ≤ 0.05, risk
  level ≤ 1), correctness, evaluation floors (critical detection ≥ 0.9, no critical case
  automated, ≥ 5 benchmark cases per critical label), the benchmark and card regenerated and
  compared, a retrained model byte-identical, the regex probe (replaced in B11 by an RE2 compile
  check), size caps (2 MB, 200 files),
  version and CHANGELOG against the digest, PHI heuristics as warnings. A unit test per rule.
  Built: `src/mendari/community/` (`check.py` the rules, `regex_safety.py` the static check and
  the subprocess probe (removed in B11), `phi_scan.py`, `maintainers.py`, `gitbase.py`), tests in
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
  "Owner" is the pull request's author (`pull_request.user.login`, never `github.actor`) equal to
  `github.repository_owner` (since the move to mendari-hq, the person the base's CODEOWNERS
  names: see that section), from a branch of this repository. Review fixes: only the owner
  removes a pack; a git submodule under `community-packs/` fails; an update is gated on the
  base's benchmark dataset too; composite members' models are retrained; no BLOCK rule, no
  Mendari name, FHIR systems under the pack's own url.
- [x] **B6 Scaffold.** `mendari new --community` writes the community defaults, and the pack's
  benchmark and card, so it passes `community-check` as written (once listed in
  `MAINTAINERS.yaml`).
- [x] **B7 Docs and templates.** `docs/guides/contributing-a-pack.md` (the flow, synthetic data
  without Python, `ABSTAIN` carries no recommendation, a risk-level table with the inbox router
  as the level-0 precedent, a pattern guide); `.github/PULL_REQUEST_TEMPLATE/pack.md`; a
  "Propose a pack" issue form; CONTRIBUTING on the DCO and content provenance;
  `community-packs/README.md` and `MAINTAINERS.yaml` (removed ids stay as tombstones; both
  written with B4).
  Built: the guide (with the risk-level table; the authoring guide and both `mendari new`
  templates now call message routing level 0, as the inbox router is); `scripts/synthetic_cases.py`
  (templates to JSON Lines, `tests/test_synthetic_cases.py`); the pack PR template and the
  "Propose a pack" form; CONTRIBUTING (DCO, content provenance); ADR-0007; threat model S13,
  T5, I12, D8, E10 and boundary 7; SECURITY.md "Community packs"; mkdocs nav, README and the
  docs home. `mendari new --community` also prints the guide's URL.
- [ ] **B8 (owner, steps below)** Labels (`pack`, `pack:new`, `pack:update`, `needs-clinical-review`,
  `clinical-review:provided`, `risk-level-change`, `patient-safety`, `idea`, `good first pack`);
  the `dco` check (built in LP-13); `pack-check` and `dco` required; a `main` ruleset requiring a code owner's
  approval, with the admin on the bypass list.
- [x] **B11 (build, before B9) Safe by construction.** Decided 2026-10-05 after three checks of
  the pattern and name checks each found new ways past them (14 open findings after #129: slow
  patterns that pass the static check and the probe, look-alike names the tables miss, and
  pack settings that multiply work). A community pack's patterns (rules and its input schema's)
  run on RE2 (`google-re2`), which never backtracks, so no pattern can stall a worker; a pattern
  RE2 cannot compile (lookarounds, back references) is refused, and the static check and probe
  are dropped for community packs. Caps: `text_fields` without repeats, `negation_window` at
  most a few hundred characters, input schemas whose `$ref`/`allOf` cannot multiply the
  patterns run; keys counted only where a schema runs patterns on them. Look-alike names become
  a warning for the owner's review, with a vendored UTS #39 confusables skeleton, and a
  reserved word counts only as whole words. One focused check, then merge.
  - [x] Stage 1, the engine (`claude/community-re2`): a community pack's rules and input-schema
    patterns run on RE2 (`core/patterns.py`, rewritten to mean what `re` means by
    `core/re2_syntax.py`; the differences left are in the guide); what RE2 cannot run is refused
    at load; every other pack stays on `re`, decisions identical. Open: RE2's time is the text's
    length times the compiled size (about 0.6 s for 9,000 counted copies on 10,000 characters),
    so a cap on a pack's total compiled size belongs with the caps (built: the review fixes).
  - [x] Stage 2, the caps (`core/caps.py`), refused at load and so by `community-check`: each
    field once in a `text_fields` (rules and dosing), `negation_window` at most 200 characters
    and at most 50 `negation_cues`, at most 50 schema patterns run on one value or key (counted
    through `$ref` and every applicator, memoised; a reference cycle reaching a pattern is
    refused), and at most 500 patterns run by the rules on an input (per question and composite
    member) and the schema on one value together. Keys count toward the 10,000 characters only
    where the schema runs patterns on them. Open, measured within the caps: the negation check
    costs each negated match its window (500 negatable patterns on "no a no a ...": 4 s at a
    window of 40, 9 s at 200; memoising `_negated` per text, or answering it from cue and break
    positions found once, would fix it); typo tolerance compares each word with every keyword
    sharing its first letter (40,000 keywords in 500 patterns: 25 s); and the compiled size
    (capped in the review fixes below). All three are bounded by the time budget (below).
  - [x] Stage 3, the check, names and docs: `community-check`'s `patterns` rule is that every
    authored pattern compiles on RE2 within the load's caps (2,000 characters, 500 a pack): the
    loader raises `PatternsRefused` naming them, and the check reports them under `patterns`;
    `community/regex_safety.py` (the static check and the probe) and its tests are deleted.
    Names (`check.name_findings`): an exact copy of a maintained pack's name or CDS Hooks title,
    or Mendari/OpenHealth as whole words (ASCII letters and digits after NFKC and case folding,
    CamelCase split, consecutive words joining, a match starting and ending at word
    boundaries), is an error; look-alikes (other alphabets, `l`/`1`/`I`, `0`/`O`, negative
    enclosed letters) and words mixing alphabets (but Latin with Greek, or with Japanese,
    Chinese or Korean scripts) are warnings saying what the name looks like. Circled,
    parenthesized and squared letters fold to ASCII under NFKC, so they are errors. The last
    review's name misses (U+01C0 and other Latin look-alikes, Cherokee, Lisu/Greek/Cherokee
    mixes, Arabic or Hebrew inside a Latin word) now warn; the tables stay incomplete on
    purpose, and no UTS #39 table was vendored (a warning for review needs none). Docs: the guide's patterns section for RE2, ADR-0007's consequences, threat model
    D8 and S13, SECURITY.md, `community-packs/README.md`. Typo tolerance's keyword list and the
    negation check's work per negated match were left uncapped here; the time budget (below)
    bounds them.
  - [x] Review fixes (the focused check's 10 findings): the size of the patterns one input runs
    is capped at 3,000 (`re2_syntax.size`, `caps.pack_size`; 148 s before for 500 patterns of
    size 2,001), and `community-check` prints it;
    case is ignored exactly as `re` does (classes written out from `re` itself, RE2 folds
    nothing: Turkish `İ`/`ı`, `\W` and Greek `ι`); word boundaries next to letters outside
    ASCII documented as they behave, with a `community-check` warning; RE2, the rewriting and
    the caps imported only for a community pack; refusal advice for back references and
    negative lookbehinds; the schema cap names `patternProperties` with `additionalProperties`;
    `unevaluatedProperties` with `patternProperties` documented; "names Mendari" capitalised;
    stale "timed" wording gone.
  - [x] The time budget (the focused check of the fixes found the stated 3.5 s worst case wrong:
    a crafted pack within every cap took 5.5 s, 17.5 s with negated patterns). A community
    pack's patterns share one second per decision (`patterns.time_budget`, set by
    `Decision.decide`): checked before each RE2 search, at each match and at each word typo
    correction looks at; past it the rules fail with `time_budget` (the decision abstains, for a
    person) or the schema check stops. Superseded by the next two items (CPU time, every schema
    keyword, abstain, about 3.5 s of CPU at worst). `tests/test_community_budget.py`.
  - [x] The budget's focused check (8 findings): the budget is the decision thread's CPU time
    (`time.thread_time`), so a model's latency or a busy server no longer spends it; every
    keyword of a community pack's schema checks it (a `$ref` fan-out with no pattern took 53 s,
    an `enum` on many items 8 s); a schema that runs out abstains, for a person, instead of
    refusing a valid input; `uniqueItems` is refused in a community pack's schema; `mendari
    test` and `community-check` print provider errors and say when a case ran out of time; the
    bound is stated as measured (one search about 2.5 s, so about 3.5 s of CPU at worst); the
    guide no longer says nothing times patterns.
  - [x] The fixes' focused check (12 findings): the budget is checked at every schema jsonschema
    applies (a subschema with no keyword on many items, 15.7 s, never checked before), not per
    keyword (fewer frames: 200-deep input crashed); `unevaluatedProperties` and
    `unevaluatedItems` refused (jsonschema walks the schema for them outside any keyword: 43 s);
    the community validator keeps its class on a `$ref` back to the root (jsonschema picked its
    own by `$schema`, patterns on `re`); an input holds at most 10,000 values nested at most 32
    deep; a schema that cannot check an input (OverflowError, RecursionError) refuses it, for
    every pack, instead of a 500; errors found before the budget runs out still refuse the
    input; `mendari validate` checks test inputs within the budget; the refusals look only at
    schema positions (a property named `uniqueItems` is fine); messages say "patterns and schema
    checks"; the spec states the budget.
  - [x] That check's focused check (12 findings: a `$ref` into `default` or `const` brought refused
    keywords back, `contains` and a long `required` list did work no check saw): a community
    pack's input schema now uses only keywords whose work Mendari bounds (an allowlist; the
    guide lists them), `$ref` only to the root's `$defs`, `required` at most 100 names, `enum` at
    most 500 flat values, `const` flat; a schema check checks the budget after each error and
    stops at 100 errors; `Decision.validate` called alone (`mendari omop`) brings its own
    budget and input limits; a reason whose wording would search patterns again falls back to
    plain words; `mendari validate` reports inputs nested too deep or holding numbers too large
    instead of a traceback; the refusal for those says so in plain words.
  - [x] The allowlist's focused check (12 findings): a `$defs` name holding `/` or `%2F` let a
    `$ref` reach a definition's data (refused keywords back); `$defs` names are now letters,
    digits, `_`, `.` and `-`. The 100-error stop, de-duplicated and shortened reasons apply to
    community packs only (every other pack's reasons are as before); `required` names and
    `enum`/`const` texts are at most 200 characters; `Decision.validate` called alone applies
    the text cap too; refusals say what to write instead; the guide and spec say an input the
    schema had already found wrong is refused.
  - [x] Its focused check found no high or medium problem (no route left to `re`, every other
    pack's reasons as on main for 70 malformed inputs). Its 3 low findings: a community pack's
    `enum` and `const` errors carry a short message (jsonschema's own copied every allowed value
    for each failing value: about 225 MB in one decision inside `anyOf`); a root `$id` that is
    not text is refused, for every pack, instead of failing the load; a refused keyword says
    why and what to write instead (`example`: `examples`, `nullable`: a type list with
    `"null"`, an annotation of one's own: `x-<name>`).
- [ ] **B9 (owner, steps below)** `MENDARI_COMMUNITY_PACKS=on` in Vercel Production
  only; on the owner's own branch preview (never a fork's), check that `community-packs/`
  reaches the function bundle.
- [ ] **B10 (owner and build, steps below)** Dry run from a second GitHub account, including a bad version
  (a pattern RE2 cannot run, a shadowing id, an edit outside its folder, a faked benchmark) that CI must
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
     `testpypi` (the rehearsal) and `ghcr` (`release.yml` publishes the images from it). Check
     after: all three list one reviewer and the `v*` tag rule. A publishing job refuses to run
     while its environment has no reviewer.
     (On GitHub Free, protection rules work only once the repository is public.)
   - Settings → **Rules → Rulesets** → New ruleset → **New tag ruleset**: name "release tags",
     Enforcement Active, Bypass list: Repository admin; Target tags: include `v*`; rules:
     **Restrict creations**, **Restrict updates**, **Restrict deletions**. Check after: the
     ruleset shows Active with those three rules.
   - pypi.org → Your account → **Publishing** → Add a new pending publisher (GitHub): PyPI
     project name `mendari`, owner `mendari-hq`, repository `mendari`, workflow
     `release.yml`, environment `pypi`. Check after: it is listed as a pending publisher, and
     the name `mendari` is still free on PyPI. The same on **test.pypi.org** with environment
     `testpypi`.
8. **Release 0.1.0.** First a rehearsal on TestPyPI, once the repository is public (`release.yml`
   publishes to TestPyPI only from a public one; going public publishes the tests' SNOMED CT
   codes and the history, LP-13 and LP-14's counsel item; the packages carry none since the
   SNOMED CT swap): a build PR sets the version to `0.1.0rc1` everywhere it is
   written (`tests/test_release_gate.py` lists them; `CHANGELOG.md`'s `## 0.1.0` section serves
   it) and pins the rc's fixed FHIR resources, which carry the version (`PINNED` in
   `tests/test_terminology.py`; its failure prints the entry), then tag it
   `v0.1.0rc1`, and Actions → **release** → Run workflow → "Use workflow from" → **Tags** →
   `v0.1.0rc1`, publish **on**, target `testpypi`; approve the `testpypi` deployment. Check
   after: the `verify-testpypi` job passed (TestPyPI serves the tested wheel byte for byte, and
   it installs and runs `mendari test`). Then, on an up-to-date `main` back at `0.1.0`, with
   `CHANGELOG.md`'s `## 0.1.0` section as you
   want it: `git tag v0.1.0 && git push origin v0.1.0` (a tag push starts nothing). Then Actions →
   **release** → Run workflow → "Use workflow from" → **Tags** → `v0.1.0`, publish **off**; it checks the tag and the version,
   runs the tests and builds and tests the packages and the six images (amd64 and arm64). When it
   is green, run it again from the same tag with publish **on** and target **`pypi`**, and in the
   run approve the `pypi` deployment and the six `ghcr`
   deployments (the images wait on the image licence compliance PR, owner's decision
   2026-10-07; once it is in, each `ghcr` deployment pushes `<tag>-sources`, then the image).
   Check after: pypi.org/project/mendari shows 0.1.0
   with a wheel and an sdist; in a fresh venv, `pip install mendari` then `mendari test
   mendari/patient-inbox-router` passes; the release job's "The sdist holds only what a source
   build needs" step passed; GitHub → the `mendari-hq` organization → Packages lists the six `mendari-*`
   images (make each public in its package settings if it is private).
9. **Docs on GitHub Pages** (the deploy workflow is `docs.yml`, LP-15c). The docs host is
   GitHub Pages at `https://mendari-hq.github.io/mendari/` (confirmed by the owner on
   2026-11-07, in chat: "the patent gate is clear and I approve everything on my part"); a custom
   domain would need Pages → Custom domain, a DNS CNAME to `mendari-hq.github.io`, HTTPS
   enforced, and a build PR that changes `site_url` and the dashboard's `DOCS_SITE`, which a
   test keeps equal. The organization is on GitHub Team, so Pages can publish from the private repository and the site would be public to
   anyone, but `docs.yml`'s deploy job skips while the repository is private: a run before A9 builds
   the site and publishes nothing. After A9: Settings → **Pages** → Source: **GitHub Actions**; then
   Actions → **docs** → Run workflow, from `main`. Check after: the site opens at
   `https://mendari-hq.github.io/mendari/` (mkdocs.yml `site_url`; for a custom domain, change it
   and add the domain in Pages), every page shows the notice, then a build PR points the README and
   `mendari new` at it and switches the install line to `pip install mendari`.

### Community packs: owner steps (B8–B10)
After the community-packs PR (`claude/community-packs`) is merged and the repository is public
(A9). Each step says where to click and what to check after. Nothing here is done by a build
session.

1. **B8, labels.** Issues → Labels → New label, for each of: `pack` (pack content and
   proposals), `pack:new`, `pack:update`, `needs-clinical-review` (no clinician named in the
   PR), `clinical-review:provided`, `risk-level-change`, `patient-safety` (blocks every other
   change to the pack until answered), `idea`, `good first pack`. Check after: open "New issue"
   → "Propose a pack" from a second account; the issue gets `pack` and `idea`.
2. **B8, the DCO check.** Nothing to install: CI's `dco` job (LP-13, `scripts/check_dco.py`)
   checks every commit of a PR. Check after: a PR with an unsigned commit shows a failing
   **dco** check whose log says how to sign off; one signed with `git commit -s` passes.
3. **B8, required checks and code-owner review.** Settings → Rules → Rulesets → New ruleset →
   **New branch ruleset**: name "main", Enforcement **Active**, Bypass list → Add bypass →
   **Repository admin** (Always allow), Target branches → **Include default branch**. Rules:
   **Require a pull request before merging** (Required approvals 1, **Require review from Code
   Owners** ticked), **Require status checks to pass** → add `pack-check` and `dco` (they appear
   once each has run on a PR; add `secrets` too if you like), **Block force pushes**. If the
   classic branch rule on `main` (Settings → Branches) already requires checks, keep it or move
   its checks here, so the two don't disagree. Settings → General → Pull Requests: **Allow
   squash merging** on. Check after: a PR from a second account shows "Review required" from a
   code owner and the two required checks; your own PR can still merge with "Merge without
   waiting for requirements" (the bypass).
4. **B9, turn community packs on in Production** (once B11, `claude/community-re2`, is merged;
   the residual risks in threat model D8 are yours to weigh). Vercel → the engine's project → Settings →
   Environment Variables → Add: `MENDARI_COMMUNITY_PACKS` = `on`, Environment **Production**
   only. Redeploy production. Check after: signed in, `/api/health` shows `packs_by_origin`
   with only `maintained` while `community-packs/` holds no pack, and `invalid` is empty. To
   turn every community pack off later, set it to `off` (or delete it) and redeploy; never use
   `MENDARI_PACKS` for that (naming a missing pack stops startup).
5. **B9, check that `community-packs/` reaches the function bundle** (not verified yet; on your
   own branch, never a fork's). In a clone: `git switch -c bundle-check origin/main`,
   `uv run mendari new community-packs/bundle-check --community --publisher "Bundle check"`,
   add `{id: community/bundle-check, maintainers: [Junebugg1214]}` to
   `community-packs/MAINTAINERS.yaml`, commit and push (no PR needed). Vercel → Settings →
   Environment Variables → Add: `MENDARI_COMMUNITY_PACKS` = `on`, Environment **Preview**,
   Git branch **bundle-check** (the branch-specific option). Redeploy that branch's preview,
   then `vercel curl /api/health --deployment <preview url>`. Check after: `packs_by_origin`
   has `"community": 1`, `invalid` is empty, and `vercel curl
   /api/packs/community/bundle-check --deployment <preview url>` shows `"origin": "community"`.
   Then delete the branch-specific variable and the `bundle-check` branch. If the pack is
   missing, either `community-packs/` did not reach the bundle (`vercel.json`) or the function
   imports Mendari from somewhere other than the repository's `src/`, so `core/registry.py`
   looks for `community-packs/` beside the wrong folder (it is found next to `src/`, unlike the
   reference packs, which the wheel also carries). Tell a build session, and leave
   Production's switch off until it is fixed.
6. **B10, the dry run** (owner and build). From a second GitHub account: fork, follow
   `docs/guides/contributing-a-pack.md` for a small pack (`community/dry-run`), open the PR with
   `?template=pack.md`, approve its first workflow run, and check that `pack-check` and `DCO`
   pass and the job summary shows the metrics and the card. Then push, one at a time, and
   check that each fails with the rule named (revert each before the next): a pattern
   `(?<!no )refill` in `logic/rules.yaml` (patterns: RE2 has no lookbehind); the id `mendari/patient-inbox-router` (identity);
   an edit to the repository's `README.md` (scope); a hand-edited number in
   `evals/benchmark.json` (honest numbers); a commit without `-s` (DCO). Merge the good
   version, then check production: `/api/health` has `"community": 1` and an empty `invalid`,
   the dashboard picker lists it under "Community · not clinically reviewed", and one Try it
   decision ends in human review with a recommendation. Remove it with a removal PR (delete the
   folder, keep its `MAINTAINERS.yaml` entry with `removed:` and `reason: dry run`), unless you
   want to keep it. Then announce.

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

## Done: the move to the `mendari-hq` organization (2026-10-08)
The repository moved from `Junebugg1214/mendari` to `mendari-hq/mendari` (still private; the
old name redirects, and the repository id, which Vercel's link keeps, is unchanged).
- [x] Links to the repository, the security contact (`SECURITY.md`, `security.txt`), the docs
  site (`https://mendari-hq.github.io/mendari/`, mkdocs.yml `site_url`) and the image names
  (`ghcr.io/mendari-hq/…`: `release.yml` pushes to `github.repository_owner`, owner's choice)
  name the organization; nothing published, tagged or dispatched (#211)
- [x] Kept: `@Junebugg1214` as the code owner and maintainer (a person; the organization has no
  teams); dated records in `notes/` (old links redirect)
- [x] Follow-up PR (#212): packs.yml passes `github.repository_owner` as the owner. Since the move
  that is the organization, which no pull request's author is, so the owner's pull requests could
  no longer remove a community pack or change `community-packs/` with other files (failing closed).
  `scripts/community_check.py` now takes the owner from the `*` rule of `.github/CODEOWNERS`, read
  from the base (a pull request's own copy could name its author), one person only (a team, an
  email, several people or a symbolic link name no owner), and only for a pull request into the
  default branch (another branch's CODEOWNERS is anyone's who may push); `--owner` is gone, and the
  fork rule stays.
- [ ] **Owner:** install the Vercel GitHub app on `mendari-hq` and reconnect `openhealth-hub`
  (its link still names `Junebugg1214/mendari`): `vercel git connect`. `mendari-web` has no Git
  link, so the new `security.txt` reaches mendari.co only when it is deployed by hand
- [ ] **Owner:** `main`'s protection under the organization: the five required checks carried
  over, but *Do not allow bypassing* (admins) is off, though `tasks/lessons.md` says it was on;
  no ruleset requires a code owner's review (`.github/CODEOWNERS` assumes one)
- [ ] **Owner:** Actions secrets and variables (present: `NEON_API_KEY`, `NEON_PROJECT_ID`),
  environments (present: Vercel's `Preview` and `Production`), and that preview and production
  deploys still start from pull requests and `main`

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
- [x] The mendari.co redesign (2026-10-11, this branch: `claude/mendari-co-redesign`), built,
  deployed 2026-11-07 (below): one identity ("cobalt on paper": the faceted decision-diamond mark, cobalt plus
  the four outcome colours, Newsreader, Public Sans and IBM Plex Mono self-hosted in
  `web/fonts/` and listed in `NOTICE`; CSP gains `font-src 'self'` only). The page replaces the
  coming-soon page: the four outcomes, the Inbox Router provider scoreboard, the five reference
  packs with their benchmark results, the three audiences, the standards and the quickstart.
  `web/index.html` and `web/style.css` now carry their licence line, so `LICENCE_LATER` left
  `tests/test_shipped.py`. A pre-launch review (security policy, accessibility at 320 to 1440 px in
  both themes, every claim against the repository, the diff) found 31 issues; the build's are
  fixed, and the owner's are below.
  - [x] **Owner, before `cd web && vercel deploy --prod`:** gate G1-2 (`patent-decision`): the
    provisional-patent decision with counsel comes before any public disclosure, and this page
    discloses more than the coming-soon page did. On 2026-11-07 the owner said, in chat, "the
    patent gate is clear and I approve everything on my part". Its record is gate G1-2's
    `patent-decision` attestation (`launch/gates.yaml`), which only the owner fills, in a
    signed-off PR before the page goes out: the decision, and the serial number if filed, with
    `by` and `date`. Not ticked until then. Later on 2026-11-07 the owner picked the decision,
    made with counsel: no provisional patent filed before public disclosure, and wrote "consider
    patent as attested"; recorded in the attestation at the owner's instruction (2026-11-07).
    The page waits for this attestation only, not the rest of G1-2 (owner, 2026-11-07).
  - [ ] **Owner:** the scoreboard names Jev (TypeSafe), Claude and gpt-oss results: confirm each
    provider's benchmark-publication terms, and state any commercial relationship (or none) next
    to it (the vision audit's LIC.benchmark-terms). Not ticked: the page as it stands names no
    remote result (17g), so this does not apply to it now; it applies again once 17i's results
    are published (LP-17 decision 8).
  - [x] **Owner:** `web/.well-known/security.txt` (already in `web/`) names the private reporting
    form, which the public cannot reach while the repository is private: keep it, or leave
    `.well-known` out of the deploy until the repository is public. Kept (owner, 2026-11-07): it
    deploys as is, and its links work once the repository is public.
  - [x] Deploy `web/` to production (Vercel project `mendari-web`): the owner asked Claude, on
    2026-11-07, to run it once `patent-decision` is attested and main is green. Inside `web/`:
    link the project, then `vercel deploy --prod` (never `--cwd web`), then check the headers and
    `/.well-known`. Done 2026-11-07, about 6:30 pm EST, from main `8dcf911b`, after main's CI
    passed and `patent-decision` was attested: the served `index.html` is byte-identical to
    `web/index.html`, and `security.txt` was kept (the owner's choice, above).
  - [ ] Later: the page's figures are typed in, not read from `packs/*/evals/*.json`; a test that
    ties them together would catch drift when the evaluations change.
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
- [x] Da Vinci CRD: `coverage-information` on the PA pack's order-sign response
  (profile-validated against CRD 2.2.1 with the HL7 validator, offline)
- [x] Da Vinci DTR: questionnaire package for the PA pack's gaps (profile-validated against DTR
  2.2.0 with the HL7 validator, offline)
- [x] Da Vinci PAS: request Bundle and submission to a mock payer (profile-validated against PAS
  2.2.1 with the HL7 validator, offline)
- [ ] **Da Vinci verification** (brief of 2026-10-17, DV-a to DV-e). Mendari's DTR route is the
  payer side (the "DTR Server"), so a payer never calls it: the counterpart is a DTR client, the
  Inferno DTR test kit v0.18.0 (suite `dtr_payer_server_v220`, Inferno as the client).
  - [x] DV-a: `$questionnaire-package` error semantics. A questionnaire not found (a version
    other than the pack's too) is a warning (`not-found`) in `outcome`, with packages or without
    (oper-9), and so is an order or a context that names no decision; the packages are the union
    of the orders' and the questionnaires asked for, each version once. Orders or a context
    that name no decision of the caller's organization, with nothing to return, are a `422`
    OperationOutcome (`not-found`, spec-130), one answer for an unknown id and another
    organization's; a decided order that needs no documentation keeps its information note
    (oper-8); a request that names no order, context or questionnaire is a `400` (oper-6).
    "Validated against DTR 2.2.0" now says profile-validated (HL7 validator, offline).
  - [x] DV-b (#266): the implementation guide packages are pinned by checksum.
    `scripts/fhir_packages.lock` names the 55 packages the validator loads, each with its sha256
    and URL (packages.fhir.org; packages2.fhir.org for VSAC 0.18.0 and 0.19.0, which only it
    serves): US Core 6.1.0, CRD 2.2.1, DTR 2.2.0, PAS 2.2.1, what validator 6.10.4 loads by
    itself, and every dependency of theirs (US Core 3.1.1 and 7.0.0 for DV-c among them).
    `fhir-conformance` checks each with `sha256sum -c`; each validator run gets a package cache
    of its own from them, with only what it loads and what that needs
    (`scripts/fhir_packages.py install`, which refuses a lock that is not exactly the closure),
    and runs with no HTTP access (`--offline`). So the US Core run holds US Core 6.1.0 alone: a
    resource that cites a Da Vinci canonical cannot pull DTR's tree (US Core 7.0.0 among it)
    into it, and the CapabilityStatement, which cites DTR 2.2.0's operation, is checked in the
    Da Vinci run. The Da Vinci guides go in as local `-ig path.tgz`;
    `tests/test_workflow_safety.py` fails a workflow that names a package by version alone, in
    any form. DV-c can build Inferno's validator a cache the same way
    (`install --cache DIR NAME#VERSION ...`, only what it loads).
  - [x] DV-c (#265): the Inferno DTR payer-suite job, `conformance.yml` (job `dtr-payer`),
    weekly and by hand, not a required check: groups 1, 3 and 4 against the open engine on
    synthetic data, from Mendari's own `sandbox/inferno/Dockerfile` (Ruby by digest, the kit by
    commit, Bundler by sha256, gems from a lockfile with CHECKSUMS) and compose file (every image
    by digest). `scripts/inferno_dtr_inputs.py` builds the inputs over HTTP;
    `scripts/inferno_check.py` compares every test with
    `sandbox/inferno/dtr_payer_server_v220_expected.json` (pass, fail, skip or omit; requirement
    ids and a reason for each that is not a pass) and fails on any difference, an unexpected pass
    included. Run once locally end to end (2026-10-18, offline terminology): 3.6.01 and 3.6.02
    pass; known gaps 1.01 (conf-1), 3.4.01 (spec-17, no `enableWhen`), 3.4.04 (spec-54), 3.5.01
    skipped (oper-5), 3.6.03 (spec-147, no `$next-question`), 3.7.02 to 3.7.04 (conf-2) and 4.01
    (oper-1); the adaptive, CQL, ValueSet and Binary tests are `omit`. No Mendari defect found
    inside the DTR route. Inferno's validator still fetches its guides from packages.fhir.org
    unpinned; DV-b's checked cache is not mounted into it yet (below).
    - [ ] Follow-up: record the URL of the first green `conformance.yml` run on main.
    - [ ] Follow-up: mount a cache built by DV-b's `fhir_packages.py install --cache` into
      Inferno's validator, so it fetches nothing unpinned.
  - [ ] DV-d (optional): the Inferno PAS client suite's approval workflow, the closest stand-in
    for a payer, attested from the decision's `pas.submitted` event.
  - [ ] DV-e is LP-23's 23l: DTR group 2 (Backend Services) and the CRD server suite, once
    LP-23 verifies EHR tokens.
  - Conflicts, defaults taken: the kits validate through tx.fhir.org, so `fhir-conformance`
    stays the offline gate (`-tx n/a`), and Mendari's Inferno run turns the terminology server
    off for every validator in the kit (`sandbox/inferno/offline_terminology.rb`, DV-c), so it
    sends nothing to tx.fhir.org; Mendari pins the kits' images and gems itself;
    `$next-question`, `$log-questionnaire-errors`, `ValueSet/$expand` and CQL population stay
    listed gaps, not advertised; no new launch gate; the HL7 DTR reference app is never run (it
    would POST to https://mendari.co).
  - **Owner decisions.** Confirmed by the owner on 2026-11-07 (in chat: "the patent gate is
    clear and I approve everything on my part"), each its default: a weekly job may send
    synthetic resources to tx.fhir.org (`terminology.yml`, weekly and by hand, never on a pull
    request, LP-23's conflict 54, merged; the offline `fhir-conformance` stays the required
    gate); the DTR payer service stays a declared partial one, its gaps listed, not advertised.
    Whether PAS attaches the completed QuestionnaireResponse (`Claim.supportingInfo`) for
    `needs_attention`: yes (owner, 2026-11-07), built (#286). Real acceptance is the owner's alone: a
    payer's PAS sandbox with credentials, a payer's written requirements, an EHR's DTR client
    (B38).

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
  reviewed by a clinician (from v0.2; the generation tooling can be built without one). The
  owner asked on 2026-11-07 for the generation tooling to be built now, for the owner's review;
  nothing claims a clinician's review until then.
  - [x] Generation tooling built (#285): `mendari.sandbox.synthetic_inbox_harder` and
    `scripts/harder_inbox_draft.py` write the draft to `datasets/drafts/patient-inbox-harder/`
    (436 cases a split, 152 urgent; leakage and coverage checks pass;
    `tests/test_synthetic_inbox_harder.py`). Draft, not clinically reviewed; the pack's data,
    model, benchmark and digest are unchanged.
  - [ ] Owner: review the draft (`notes/clinician-review-harder-inbox-benchmark.md`: what to read,
    seven quick-pick questions), then decide its adoption, which a separate PR carries out
- [ ] Trial pack: Jev-alone leaderboard rows on test and the local site (about 200 paid Jev calls;
  only when asked)
- [x] Try it on record packs: a form generated from the pack's input schema (`form.js`: fields,
  dropdowns and date pickers, editable tables for lists, JSON for anything else), with the JSON
  one click away; every behaviour-test example of the four record packs round-trips unchanged
- [x] (PR open) Try it reaches the three pack routes it had no screen for, each only where the
  pack takes it: "Check the AI reply" (`…/guard`, a pack with a `guard` section; the answer now
  carries the whole `decision`, so the result reads as a decision does, headed by what to do
  with the reply); an input switch "Form | (JSON |) FHIR" (`…/fhir/decide`, a pack with a FHIR
  mapping; pasted JSON checked to parse first, an OperationOutcome said in words under it);
  and under a complete prior-auth request (`pas_ready` in the pack detail, `pas.READY`) the PAS
  request built from pasted resources (prefilled from FHIR input), a refusal naming a
  `MENDARI_PAS_` setting opening the fields the route takes, the Bundle to copy or download.
  No X12 value in the dashboard. Tests: `tests/browser/test_try_modes.py` (with axe, both themes)
- [ ] Try it's FHIR input starts empty: no pack ships a FHIR example of its own (the API
  reference's is the inbox router's, in `server/api_docs.py`); a `…/fhir/example` would prefill it
- [ ] VoiceOver spot check of the redesigned dashboard (a person at the Mac; script in the QA report)
- [ ] (PR open) The audit routes on screen: the Audit trail shows the signed daily checkpoint
  (21k; day, head, key id, "Save a copy" of the route's answer; "not set up" without a key,
  "none signed yet" before the first) and, for administrators, "Export as FHIR" (the pack's
  trail, `audit-<pack>-<day>.ndjson`, fetched to carry the organization); the Access view's
  Activity tab (12g-ii, administrators signed in): the organization's administrative events of
  the last 90 days, newest first, in words (event, about what, by whom, when), asked for each
  time it is shown, and their FHIR export. One server addition: `/api/health` names the
  checkpoint key's id (`checkpoint_key`, or null), so the dashboard asks for no checkpoint, and
  logs no 404, where none is set (tests/browser/test_audit_activity.py)
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
