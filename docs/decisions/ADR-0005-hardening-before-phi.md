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
- **Commit, don't copy.** A note (review, feedback) and payer text go to a `notes`
  table: sealed with the PHI key under `encrypt`, plain under `off`, refused (`422`) under
  `hash`, which promises to keep no free text. The chain keeps a reference and a salted
  commitment, so a note can be erased (a chained `note.erased`) and the chain still verifies.
  Notes follow inputs: the same readers, the same retention. Notes chained before this decision
  are hidden at read time from anyone without `read_input`; the chain itself is never rewritten.
  A governance approval's `reference` (an administrator's pointer to minutes) stays chained.
- **Evidence follows the input.** Under `hash` the stored record keeps no evidence values; under
  `encrypt` they are sealed with the input. A Patient id in an input issue is stored as its type
  and position.
- **The input hash is keyed.** `hmac-sha256:<key id>:<hex>` under `MENDARI_INPUT_HASH_KEY`,
  required in `hash` and `encrypt`. Without PHI, the plain `sha256:` stays. No keyring yet:
  rotation is "purge, then switch".
- **People are chained by their verified email** (or, without one, the identity provider's
  subject qualified by its issuer), machines as `key <id>` or `client <id>`. In open mode, names
  given for reviewers and approvers must be identifiers.
- **Two layers of limits.** Vercel's firewall limits per IP, only on routes anyone can reach
  signed out (sign-in, the token endpoint). Mendari limits per principal, only on costly routes
  (decide, the AI guard, evaluations, exports), in Postgres so every instance shares the count,
  with `429` and `Retry-After`. Organizations get daily decision quotas, and at launch Jev and LLM
  get finite daily ceilings (about 2,000 and 200; the owner's choice); over a quota, decisions
  fall back to human review. Reads are not limited in the app.
- **A security log on stdout, the chain for PHI.** One JSON line per API request and security
  event, from an allow-list of fields: never a credential, a query value, a body or case text.
  Reads of PHI stay chained. No new log tables.
- **Sessions:** 30 minutes idle, 8 hours at most, `__Host-` cookie names (with the old name
  accepted for one release, so nobody is signed out), sign-in and SMART launch tied to the
  browser that started them.
- **Where a request comes from decides what it may reach.** A browser request from another site
  reaches only public pages and the dashboard, and only to read. Machines with a bearer
  credential are not affected.
- **Nothing from a CDN.** The API reference is Swagger UI, vendored at a pinned version with its
  license and checksums, under the same content security policy as every other page. CI actions
  are pinned by commit, images by digest, and Dependabot proposes updates monthly.

## Consequences
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
