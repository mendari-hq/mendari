# ADR-0005: Before PHI, the chain commits to what it must not copy, and limits come in two layers

## Status
Accepted (Phase 4b, 2026-10-02)

## Context
Phase 4 gave Mendari sign-in, scoped machine access and SMART Backend Services. Before the owner
lifts Vercel Authentication, or any site handles PHI, the audit and the Phase 4b research found
gaps of three kinds:

- **What is kept forever.** The hash chain cannot be edited. Review and feedback notes are free
  text chained as is, payer text from prior authorization is chained, the stored record keeps
  evidence values (an age, a lab value and its date) even under PHI mode `hash`, and the input
  hash is an unkeyed SHA-256, which a dictionary of short messages or record numbers reverses.
  People are chained under a display name anyone can change at their identity provider.
- **What anyone can make Mendari do.** Nothing limits how often a caller signs in, asks for a
  token or makes a decision that calls a paid model. The public capability statement takes the
  chain's exclusive lock, sign-in fetches the identity provider's discovery document on every
  call, and the public health endpoint lists packs, models and today's usage.
- **What the browser and the build trust.** Sessions last 8 hours whatever the activity, cookies
  have no `__Host-` prefix, no route checks where a request came from, the API docs load Swagger
  UI and ReDoc from a CDN without a content security policy, and CI actions and images are
  pinned to tags.

The first pilot with real patients runs on the pilot site's own infrastructure, from Mendari's
images (`docs/deploying-with-phi.md`). The project's Vercel deployment stays synthetic only.
So the chain decisions below matter most for self-hosted installs, and the limits for the
public deployment.

## Decision
- **Commit, don't copy.** A note (review, feedback) and a payer's text (an authorization
  number, a disposition) go to a `notes` table: sealed with the PHI key under `encrypt`, plain under `off`, refused (`422`) under
  `hash`, which promises to keep no free text. The chain keeps a reference and a salted
  commitment, so a note can be erased (a chained `note.erased`) and the chain still verifies.
  Notes follow inputs: the same readers, the same retention, and a read is chained in the same
  `input.read`. Notes chained before this decision are hidden at read time from anyone without
  `read_input`; the chain itself is never rewritten. A governance approval's `reference` (an
  administrator's pointer to minutes, not case text) stays chained. The FHIR exports carry
  codes only.
- **Evidence follows the input.** Under `hash` (and for a pack that keeps only the hash) the
  stored record keeps no evidence values; under `encrypt` they are sealed with the input. A
  resource id in an input issue is stored as its type and position (`Patient (entry 2)`), in
  every mode.
- **The input hash is keyed.** `hmac-sha256:<key id>:<hex>` under `MENDARI_INPUT_HASH_KEY`,
  required in `hash` and `encrypt` (the engine refuses to start without it, or with the PHI key
  reused). Without PHI, the plain `sha256:` stays. No keyring yet: rotation is "purge, then
  switch".
- **People are chained by their verified email** (or, without one, the identity provider's
  subject qualified by its issuer), machines as `key <id>` or `client <id>`. In open mode, names
  given for reviewers and approvers must be identifiers.
- **Two layers of limits.** At the edge, one Vercel firewall rule (or the site's proxy) limits
  per IP, only on the routes anyone can reach signed out: `/auth/` and `/smart/`, 120 a minute,
  never `/api` or `/fhir` (a hospital is one address). It starts in log mode for 7 days. In the
  app (`MENDARI_RATE_LIMITS`, on by default with sign-in), Mendari limits per principal on the
  costly routes (decisions 600 a minute with bursts of 100, each draft order of a CDS Hooks
  order hook counted as one and at most 50 a request; the AI guard 60 a minute, evaluations 6
  an hour, exports 10 an hour), and per address on sign-in (300 a minute, bursts of 120, at
  least what the edge allows, as a hospital signs in from one address), the token endpoint (60
  a minute) and failed assertions per address and client (10 in 10 minutes). The
  limiter is GCRA, one statement on one row per bucket in the deployment's database (SQLite or
  Postgres), so every instance shares the count; it runs after the sign-in check and before
  any body is read, model called or event chained, and answers `429` with `Retry-After`. `log`
  mode counts without refusing. Reads are not limited in the app. Organizations get daily
  quotas (`MENDARI_ORG_DAILY_LIMITS`: decisions and their remote calls), and at launch the
  project's deployment has finite ceilings (Jev 2,000 and LLM 200 a day, 5,000 decisions per
  organization a day; the owner's choice). Over a quota or a ceiling, decisions fall back to
  human review. Evaluations spend the deployment's budgets, never a quota.
- **A security log on stdout, the chain for PHI.** One JSON line per `/api`, `/fhir`,
  `/cds-services` and `/auth` request and per security event, from an allow-list of fields:
  never a credential, a query value, a body or case text. Every response carries a request id.
  Reads of PHI stay chained, and the log repeats them by decision id. No new log tables.
- **Public routes cost little and say little.** Without a credential `/api/health` is
  `{status, version, sign_in}`; the capability statement takes no chain lock; sign-in caches
  the identity provider's configuration; no body above `MENDARI_MAX_BODY_BYTES` (4 MiB) is
  read, and none before the sign-in check.
- **Sessions:** 30 minutes idle, 8 hours at most, `__Host-` cookie names (with the old name
  accepted for one release, so nobody is signed out), sign-in and SMART launch tied to the
  browser that started them.
- **Where a request comes from decides what it may reach.** A browser request from another site
  (Fetch Metadata, else `Origin`) reaches only public routes and pages, and only with GET or
  HEAD, keyed on the route permissions rather than a list. It applies to requests with a
  Mendari cookie and, in open mode, to every request. Machines with a bearer credential are not
  affected.
- **Nothing from a CDN.** The API reference is Swagger UI, vendored at a pinned version with its
  license and checksums, under the same content security policy as every other page. CI actions
  are pinned by commit, images by digest, and Dependabot proposes updates monthly.

## Consequences
- Deployments in `hash` or `encrypt` must set `MENDARI_INPUT_HASH_KEY` before upgrading, or the
  engine refuses to start.
- Rolling back past this release signs out browsers that hold `__Host-` cookies, and, once PHI
  is stored, would chain notes again. Do not roll back past it after PHI arrives.
- A self-hosted site can erase a note a reviewer should not have written, and its chain still
  verifies. An export made before the erasure still holds the note.
- A busy legitimate day can reach a quota; raising it is an environment change and a redeploy.
- A cross-site link to an API route answers `403`; links to pages work.
- Reviewers and approvers are identifiable in the chain by email, for good.

## Alternatives Considered
- **A fresh production database.** Rejected: the old rows are synthetic, a reset signs the owner
  out, and self-hosted installs need the code to read both shapes anyway.
- **Access and security events in database tables.** Deferred: stdout to the site's log system
  plus the chain covers the need.
- **In-memory rate limits.** Rejected: serverless instances share nothing.
- **Scalar or ReDoc as the reference.** Rejected for now: Scalar is a 4.4 MB bundle with
  telemetry and a proxy on by default; ReDoc needs a `blob:` worker, loads its logo from its own
  CDN and has no "Try it". Swagger UI passes the dashboard's content security policy unchanged.
- **Display names or opaque ids in the chain.** The owner chose verified emails.
