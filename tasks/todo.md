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
  Owner question still open: refuse an explicit `off` with sign-in on outside Vercel too (the
  compose sign-in sandbox relies on it)?
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
  **For counsel (not decided here):** whether SNOMED CT codes may stay in two reference packs,
  four US Core test fixtures, `datasets/trial-local-site-example.jsonl` (bare codes, shipped in
  the wheel), the sandbox generators (`src/mendari/sandbox`) and the docs' examples; SNOMED CT
  needs an Affiliate Licence, free in member territories, low-income countries and qualifying
  research, paid elsewhere; replacing them means changing two packs' codes, and the US Core
  fixtures code procedures and referrals in SNOMED CT. What ICD-10-CM's terms need (free to use
  from NCHS, adapted from WHO's copyrighted ICD-10 for U.S. government purposes). What
  distributing the LGPL-3.0 PostgreSQL driver inside the published container images requires:
  psycopg-binary bundles libpq, OpenSSL 3 and 1.1.1, libxcrypt and keyutils (LGPL-2.1+), Cyrus
  SASL and MIT Kerberos (attribution clauses), OpenLDAP, PCRE2 and libselinux, whose licence
  texts the image does not carry beside psycopg's own, and the image label says only Apache-2.0.
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
    `docs` job). `site_url`: `https://junebugg1214.github.io/mendari/`. Nothing published or
    deployed. **Owner, open:** the docs URL (GitHub Pages by default; a custom domain is a
    `site_url` change), Pages needs the repository public on the Free plan; the first TestPyPI
    upload waits for counsel's SNOMED CT answer (LP-14); the `testpypi` environment and
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
  hand, as the script itself stops there with exit 2, naming the test files it lacks. A5 waits
  on LP-08's open owner question (an explicit `MENDARI_PHI_MODE=off` with sign-in starts with a
  warning outside Vercel, where A5 step 1 asks for a refusal). New evidence tests: the notice's
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
  - [ ] **LP-10c** The model that answered, on each answer and in each report; and removing
    `DEFAULT_MODEL` (an `llm` variant with no model fails as `not_configured`, in `preflight`).
    - [x] **10c-i** (#158) The model that answered: Jev already recorded it; the `llm`
      provider now stamps the id the endpoint reports (well formed only, else the configured
      one, and the call stands) on each answer, and `vote` names each member that answered with
      its model. No format bump: the spec already defined `models` as the models that answered;
      earlier `llm`/`vote` runs named the configured one (CHANGELOG).
    - [ ] **10c-ii** Removing `DEFAULT_MODEL`, owner-gated as below.
    **Owner first:** no default configuration uses an LLM, but an `llm` slot chosen through
    `MENDARI_PROVIDERS` without `LLM_MODEL` would start sending every case to a person. The last
    tracked hosted setting (removed 2026-10-04) had `mendari/ai-guard:screen=rules_and_llm`:
    check Vercel's `LLM_MODEL` for Production and Preview before that part merges.

- [ ] **LP-11** Evaluation-set store and blind labelling. **Owner first:** where hidden-set
  content, labels and keys live (outside this repository, every pack directory and every pack
  digest); the build prompt says to stop until this file records it. Not started.
- [ ] **LP-12** Accounts, memberships and invitations (ADR-0008, Proposed). Planned by a mapping
  workflow as eleven PRs, each leaving claims mode exactly as it is:
  - [x] **12a + 12b** (#159) This entry and ADR-0008; tenancy fails closed: for a signed-in
    principal of no organization (`tenancy.acting_for_nobody`), every read and write that
    resolves its organization through `tenancy.current()` is refused (`NoOrganization`, 403),
    never widened to all of them (None) or to `default`; an empty organization is one, never
    "any", in the key and client store.
  - [ ] **12c** Decide-scoped credentials list packs and read CDS discovery (ADR-0004).
    **Owner first:** it widens what a decide-scoped key or token reaches in claims mode,
    production included (the pack list and CDS discovery, read-only and deployment-wide), and
    `/api/auth/me` lists a new `discover` permission. The LP-12 prompt asks for it; the owner
    approves the ADR-0004 extension before it is built.
  - [x] **12d** (#162) User, organization, membership, invitation and request records, each
    write chaining ids and digests only; the operator's `mendari access` commands
    (organizations, bootstrap, invite, members, requests, and the read-only preflight
    `check`); runbook `docs/operations/accounts.md`. No route reads them yet. Settled in its
    review: the bootstrap's membership stays pending until the owner signs in (it never picks
    a stored person by address); an address is ASCII, lower-cased, never case-folded; only an
    administrator who has signed in keeps an organization administered; the commands that
    write refuse a protected database without its input-hash key; the preflight counts
    evaluation reports and names an id no record can have (`"recordable": false`).
  - [ ] **12e** `MENDARI_ACCOUNTS=memberships`, in two PRs.
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
    - [ ] **12e-ii** The dashboard: the no-access view with the request form, the chooser
      (whenever a person holds a membership and none is chosen), the switcher, the
      access-ended and window-moved banners, "Use another account"; browser tests.
  - [ ] **12f** Invitations and member management (no e-mail sent), in two PRs, so the server
    does not wait on 12e-ii's dashboard files:
    - [ ] **12f-i** The server: an administrator's routes to invite (the link shown once),
      list and revoke invitations, list members, change roles, remove a member, and handle
      access requests; a signed-in person's route to accept an invitation made for their
      verified address. Built on 12d's store methods.
    - [ ] **12f-ii** The dashboard: the members and invitations views, the `/invite` page;
      the browser test of the whole flow (an expired link and a wrong address included).
  - [ ] **12g** The organization bound into each chained event (memberships mode), checked by
    `verify`; per-organization administrative reader and export; the generated two-organization
    isolation test with its reviewed allow-list.
  - [ ] **12h** The Access screen; a client's key set replaced; credential history.
  - [ ] **12i** Key presets in one table (API, screen, docs, doc-snippet checks).
  - [ ] **12j** A daily cap per principal (429, `Retry-After`; off unless configured).
  - [ ] **12k** Close-out: the a6-accounts gate's probes, the README, the ADR's status.

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
     credential lists packs and reads CDS discovery, and `/api/auth/me` lists `discover`.
  7. The claims-mode no-role page stays as it is.
  8. Additive claims-visible changes count as "claims mode behaves as before".

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
  `github.repository_owner`, from a branch of this repository. Review fixes: only the owner
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
     project name `mendari`, owner `Junebugg1214`, repository `mendari`, workflow
     `release.yml`, environment `pypi`. Check after: it is listed as a pending publisher, and
     the name `mendari` is still free on PyPI. The same on **test.pypi.org** with environment
     `testpypi`.
8. **Release 0.1.0.** First a rehearsal on TestPyPI, after counsel's answer on SNOMED CT (LP-14:
   the wheel carries those codes): a build PR sets the version to `0.1.0rc1` everywhere it is
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
   deployments. Check after: pypi.org/project/mendari shows 0.1.0
   with a wheel and an sdist; in a fresh venv, `pip install mendari` then `mendari test
   mendari/patient-inbox-router` passes; the release job's "The sdist holds only what a source
   build needs" step passed; GitHub → your profile → Packages lists the six `mendari-*`
   images (make each public in its package settings if it is private).
9. **Docs on GitHub Pages** (the deploy workflow is `docs.yml`, LP-15c). Settings → **Pages** →
   Source: **GitHub Actions**; then Actions → **docs** → Run workflow, from `main`. Check after:
   the site opens at `https://junebugg1214.github.io/mendari/` (mkdocs.yml `site_url`; for a
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
