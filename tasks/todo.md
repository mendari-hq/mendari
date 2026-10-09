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
optional). Each step is one PR; Claude merges it when CI is green and the review found nothing
medium or worse, and asks first for anything touching production, secrets or a pack's clinical
behaviour.

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
  commits, clean. Gate G1-2's `secret-scan` is now met. **Owner, once public:** GitHub turns a
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
  **Owner decisions, open (the recommended defaults used meanwhile):** the copyright holder
  ("The Mendari authors" in NOTICE until you name one); publish the existing history or start the
  public repository from a fresh commit (nothing done: history untouched); whether `tasks/` and
  `notes/` go public (they stay in the repository but out of every package, the site and the
  image, enforced). Deploy `web/` again (owner) once the repository is public, so security.txt's
  contact works.

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
  would change how production installs. Kept as is, with a named exception; say if you want the
  extra (then the Vercel install must name it, checked on a preview first).
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
  and the image, so NOTICE names the files and not the values. **Open, for X12's written
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
    **Owner decisions, open (defaults used):** `monitor` keeps exit 1 for alerts, so a
    scheduler cannot tell alerts from a database it cannot read (the line says which; the
    deploy templates' `|| true` swallows both): give alerts their own code? `MENDARI_PROVIDERS`
    stays a warning in the CLI and a report on a server (production runs an unscoped setting);
    make it strict?
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
    deployed. **Owner, open:** the docs URL (GitHub Pages by default; a custom domain is a
    `site_url` change), Pages needs the repository public on the Free plan; the first TestPyPI
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
    LP-10); the registry's six writes (publish and reports, LP-18; claims, approval, tokens and
    keys, LP-19).
  - `web/index.html` and `web/style.css` (the mendari.co page) have no licence line: it goes in
    with the owner's next change there, so a comment alone does not redeploy the public site.
  - The terms, privacy notice and acceptable-use pages (gate G2-3) have no build prompt yet.
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
    differs from the installed one: a puller refuses it, with the reason.
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
  - [ ] **16g** (#219; PR E, last) The threat model's "Pack content" part
    (`docs/security/threat-model.md`, between the STRIDE table and the residual risks): what a
    pack can set (every manifest input, provider type and variant key, the wording templates,
    `cds_hooks.prefetch`, the attestations), which packs the deployment owns (by origin and
    digest), and what a pack can and cannot do, each with its row or residual (conflicts 23 and
    24 are written there). `tests/test_threat_model.py` holds it to every input, to each
    provider type's closed config keys, to rows and tests that exist, and to no line that reads
    as a row; gate a3's `what` says LP-16a–g. LP-16 counts as merged for LP-18 and LP-20 once
    16g lands, 16e-2 with it (16c-2 is not part of LP-16, above).

  **LP-16 rest, in order:** A (F and 16f, this PR); B (16d, beside A); C (16e-1, after A and B);
  D (16e-2, after C); E (16g, after A to D).

  **Conflicts written before building LP-16, and the default each PR follows** (the owner may
  choose otherwise; each default is named by the PR that depends on it):
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
  **Not done, for the owner:** attaching the sources to a GitHub release (no release is created
  today, and it needs `contents: write` on a publishing job, which the workflow safety rules do
  not allow; the `-sources` tag in the same GHCR repository is the source's place instead);
  `SOURCE.md`'s fallback ("open an issue in the Mendari repository and the maintainers will send
  it") commits the maintainers: confirm or name a contact; counsel to confirm the approach
  (equivalent access from the same place, no separate written offer). Keep each `-sources` tag
  as long as its image, and make it public with the image's package.
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
  archive, and snapshot.debian.org as pinned versions leave the pool; a scheduled job is the
  owner's call.

- [ ] **LP-10** Statistics and a per-case ledger behind every number. Split in three PRs:
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
        fact and the search cards as k of n. Its card is still raw Markdown tables (no GFM);
        left for LP-18, which reworks what the Hub shows of a version.
      - Owner, optional: give the 166 shipped `mendari.leaderboard/1` rows their accuracy
        counts from the committed datasets where provable (the dataset unchanged since the
        row's run, k/n exactly the stored rate). Without it they show accuracy as n/a.
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

- [ ] **LP-11** Evaluation-set store and blind labelling (ADR-0011, Proposed).
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
  without both keys; the full plan, its 55 conflicts with the default each PR follows, and the
  risks are in `notes/lp11-plan.md`. Guards come first: keys, permissions and a generated
  no-leak sweep land before any evaluation-set route exists.
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
  - [ ] **11c** (#222) Evaluation-set keys and sealing, nothing stored yet
    (`core/evalset_keys.py`: `MENDARI_EVALSET_KEY`, `MENDARI_EVALSET_READ_KEYS`,
    `MENDARI_EVALSET_INDEX_KEY`; off unless both are set, malformed, half-set or reused keys
    stop the server, which then needs sign-in and, on Vercel, Production). Every token names its
    key and is bound to its kind, set, case and label; case and index hashes are keyed by the
    index key. The PHI policy's sealing moved into `phi.seal_with` and `phi.open_with` with its
    bytes unchanged (tokens sealed before still open). Health shows the key ids to readers.
  - [ ] **11d** (#223; owner's go-ahead 2026-10-09) Permissions, no route needs them yet:
    `label` (the `labeller` role, memberships mode only; a label-only member sees a page saying
    so), `curate` (people; administrators, who hold neither `label` nor `grade`), and `grade`,
    held only by a grading key the operator mints (`mendari access grader-key`, at most 365
    days, chained; listed and revoked, never rotated; refused over HTTP); open mode holds none
    of the three. ADR-0004 and ADR-0008 amended; threat model E2 and E11.
  - [ ] **11e** (#224) Store I: sealed sets and cases, the hidden-case index,
    `audit verify --evalsets` (the operator's command line only): `core/evalsets.py`,
    `mendari evalset create|add|remove-case|list`, hand-written cases only (public cases refused
    exactly and under `norm/v1`), each case chained as its keyed hash and a salted commitment
    (`case_ref`), stray, retired and PHI keys refused before every write and at startup; the
    runbook `docs/operations/evalsets.md`. The sealed freeze record's column is `freeze_record`
    (FREEZE is reserved in Postgres).
  - [ ] **11f** (PR open) Key lifecycle: rotation with its check, reindexing, the sealed export:
    `mendari evalset rotate --to <id> [--batch N] [--check]` (copied from `phi rotate`: chained
    `evalset.rotated` batches, compare-and-set, retired keys seal nothing, every set state; the
    check reads only), `mendari evalset reindex` (the whole index under the current index key,
    one transaction, `evalset_index.rebuilt`, earlier index keys retired), `mendari evalset
    export` (the rows as stored: ciphertext, ids and key ids, no key, nothing chained; 11g allows
    its case ids and sealed tokens, conflict 56); `SEALED_COLUMNS` and its pin; the runbook's
    "Rotating the evaluation-set key".
  - [ ] **11g** (PR open) The generated no-leak sweep, before any evaluation-set route (tests
    only): `tests/test_evalset_leaks.py` plants a mark in every field of two organizations' draft
    sets and calls every mounted route and method as every kind of principal, scanning answers,
    headers, NDJSON, logs, every `mendari evalset` command, the audit and feedback exports, the
    sealed export file and every table (a mark only where conflicts 38, 55 and 56 allow it);
    each evaluation-set event's payload keys pinned; the route inventories (`test_auth`,
    `test_hosted_data`, `test_two_organizations` with a set per organization, `test_memberships`,
    `test_origin`, `test_shipped`, `test_limits`, and after review `test_landing`,
    `test_key_presets`, `test_rotation`, `test_capability`) run with the feature on and assert
    it; threat model I13.
  - [ ] **11h** Store II: sampling, assignment, blind labelling, adjudication, agreement.
  - [ ] **11i** Store III: freeze and its commitment, the grading read, the rotation rule,
    retirement.
  - [ ] **11j** The labelling routes (`label`).
  - [ ] **11k** The set-management routes (`curate`).
  - [ ] **11l** The grading route and the aggregates-only result (`grade`).
  - [ ] **11m** The labelling screen.
  - [ ] **11n** The management screen.
  - [ ] **11o** Close-out (gates, threat model rows, the review packet).

  **Owner decisions still open** (none blocks 11a to 11i): how labellers reach the hosted site
  (memberships mode, or `labeller` in claims mode, and past Vercel Authentication); each set's
  rotation numbers (maximum grading runs and age, both required at freeze); what settles a
  three-way adjudication split; whether remote providers may grade hidden sets (LP-17); raw or
  distinct positives in the evidence rule (LP-17); whether an administrator may also label (no,
  by default); how a hidden case is erased (before B20).
- [x] **LP-12** Accounts, memberships and invitations (ADR-0008, Proposed; built 2026-10-08, G2-3's a6-accounts prerequisite met). Planned by a mapping
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

  **Conflicts written before building, and the default each PR follows** (the owner may choose
  otherwise; each default is named by the one PR that depends on it):
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

  **Owner decisions** (none blocks a PR; each PR follows the recommended default): switching
  the hosted site to memberships mode, and when; the owner's identity for bootstrap (the
  Google-verified e-mail, bound at first sign-in, recommended); who the operator is; whether
  `seq` may show in an organization's views; refusing, in memberships mode, keys, clients and
  EHR registrations of an organization with no record (recommended); the per-principal cap's
  value; invitation lifetime (7 days, at most 30) and whether `default`'s admins may invite;
  the reserved role spellings; notice acknowledgement per person (recommended); what the local
  datasets folder may hold in memberships mode; what becomes of an organization in use whose
  id no record can have (the preflight names it; until then, stay in claims mode); switching
  the hosted site after 12g rather than 12f (recommended: until 12g an organization's
  administrators still see the whole-chain verify, deployment records and some health
  fields); whether asking to join an unknown organization should say so (default: the same
  answer, so it names no organization); recording only people a membership or a request
  concerns (default, as 12e-i does) or everyone who signs in.

- [ ] **LP-18** A registry that shows only what it checked (ADR-0012, Proposed). Planned
  (2026-10-09) as eight PRs, each merged before the next; LP-16 is merged (16a to 16g). None
  changes what a pack decides on its own engine; 18d only lowers automation for pulled packs.
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
  - [x] **18b** (PR open) The registry's chain: every write appends a chained event in its transaction
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
  - [x] **18c** (PR open) Yank as a recall, and delete (`hub/yank.py`): a `mendari.yank/1`
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
  - [x] **18d** (PR open) Origin at pull and the cap: the verified marker (`.mendari-verified`)
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
    needs.
  - [ ] **18e** A running server honours a yank: the packs it serves from a registry are
    checked against their registry's yank records on a schedule, and a yanked one (or one no
    check reached for too long) stops automating.
  - [ ] **18f** Rate limits (the `Limiter`, on the registry's own database), security headers on
    every registry route and every Hub page (CSP, HSTS, frame-ancestors), each asserted by
    exact value, and a scanner baseline in the `hub-site` job.
  - [ ] **18g** The regression check: on every publish, the pack gate (`evals/gate.py`, as
    `scripts/pack_gate.py` runs it) on the publisher's own test set, in an isolated job (a
    subprocess with time and memory limits, local providers only), against the previous
    version's registry-computed report; shown as "publisher's own test set: regression check,
    not a ranking".
  - [ ] **18h** Close-out: gate a10's probe (LP-18's tests; LP-19 stays pending), threat model
    rows, docs.

  **Defaults each PR follows** (the owner may choose otherwise):
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

- [ ] **LP-21** Operations for a public service (ADR-0010, Proposed). Planned by a mapping
  workflow (2026-10-08) as fifteen PRs, each safe alone; none changes what a valid pack
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
    never re-run; gate `hosted-monitoring` names its tests and waits for 21n.
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
    --public-key <key>`. The hosted schedule waits for 21n.
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
  - [ ] **21n** (#217, draft) The hosted schedule (`vercel.json` crons): **owner's go-ahead before merging**.
    Prepared: the purge at 08:00 UTC, the monitor at 09:00, the checkpoint at 10:00, each daily;
    a test pins them and runs each with the secret, and gate `hosted-monitoring` names it. Its
    alerts reach someone once the owner sets `MENDARI_ALERT_WEBHOOK` for Production; the gate
    stays pending until then.
  - [ ] **21o** Firewall enforcement: **owner only**, after the log-only week.

  **Defaults each PR follows** (the owner may choose otherwise):
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

  **Owner decisions** (none blocks 21a to 21m): the status page provider, its checks and who is
  paged (B36); the evaluation quota values, and whether evaluations get a deployment budget of
  their own (today they share `MENDARI_JEV_DAILY_LIMIT` and `MENDARI_LLM_DAILY_LIMIT` with live
  decisions); the alert format, secret and destination; the
  checkpoint key and where its public key is published; the hosted schedule (21n); firewall
  enforcement (21o).

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
9. **Docs on GitHub Pages** (the deploy workflow is `docs.yml`, LP-15c). Settings → **Pages** →
   Source: **GitHub Actions**; then Actions → **docs** → Run workflow, from `main`. Check after:
   the site opens at `https://mendari-hq.github.io/mendari/` (mkdocs.yml `site_url`; for a
   custom domain, change it and add the domain in Pages), every page shows the notice, then a
   build PR points the README and `mendari new` at it and switches the install line to `pip
   install mendari`.

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
