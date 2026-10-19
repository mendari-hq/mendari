# Architecture

## Components

```text
            ┌──────────────────────────── mendari serve ────────────────────────────┐
 EHR/app ──►│ REST /api/packs/{ns}/{name}/decide      FHIR /…/fhir/decide   /cds-services│
            │        │                                   │ FhirAdapter          │        │
            │        ▼                                   ▼                      ▼        │
            │   ┌─────────────────────────── Decision (runtime) ───────────────────┐    │
            │   │ input schema ─► provider slots ─► policy ─► DecisionResult        │    │
            │   │                 rules│naive_bayes│jev    rules, thresholds, caps  │    │
            │   └──────────────────────────────────────────┬───────────────────────┘    │
            │                                              ▼                             │
            │   AuditStore (SQLite): decisions · hash-chained events · review queue ·    │
            │                        eval reports                                         │
            │   Dashboard (static HTML/JS)  ◄── /api/stats /api/review /api/audit /api/evals│
            └────────────────────────────────────────────────────────────────────────────┘
```

- **Spec and pack loading** (`core/spec.py`, `core/pack.py`): pydantic models, cross-reference
  validation, file confinement, content digest.
- **Registry** (`core/registry.py`): resolves `namespace/name[@version]` from
  `MENDARI_PACKS_PATH`, the registry cache, the bundled packs and, with
  `MENDARI_COMMUNITY_PACKS=on`, `community-packs/`. Each pack carries the origin of where it
  was found (`core/origin.py`: maintained, community, hub or local); an id is one pack, and a
  second claim is reported, never served. The origin, never the manifest's `publisher.type`
  (self-declared), sets the label every surface shows: "Maintained by Mendari" or "Community ·
  not clinically reviewed" ([ADR-0007](decisions/ADR-0007-community-packs-by-pull-request.md)).
  The origin and the digest choose the engine a pack's patterns run on (`Pack.bounded`): a pack
  the deployment does not own (a community pack, or a pulled one that is not a reference pack of
  the installed release, `core/reference.py`) runs on RE2 with caps, input limits and a CPU
  budget per decision; the others on `re`, their input schemas bounded in shape at load
  (`core/input_schema.py`, over `core/schema_graph.py`)
  ([ADR-0009](decisions/ADR-0009-bounded-evaluation-for-packs-the-deployment-does-not-own.md)).
  A pulled pack never takes a reference pack's place: one with its id and version but other
  files is refused.
- **Runtime** (`core/decision.py`): binds a pack to concrete provider variants. It validates
  input, collects answers, applies policy and optionally records the result.
- **Derived questions** (`core/derive.py`): computes decision-table questions from their
  members' answers with exact probabilities (spec 0.2 §4.3), and adds expected values to score
  answers.
- **Policy** (`core/policy.py`): a pure function from answers and failures to an outcome. It
  holds all automation limits, including risk-level caps, governance overrides and the
  community-pack cap (a community pack never automates).
- **Providers** (`providers/`): a common `answer(inputs) -> ProviderOutput` contract.
  Within one decision, remote providers that share an endpoint and model are merged into a
  single call (`providers/batch.py`): the inbox router and the Jev half of its safety screen
  make one Jev call and use one budget unit.
- **Audit** (`core/audit.py`): write-once decision records. Reviews and outcomes are appended
  as events, the hash chain is verifiable, and the store also serves the review queue and
  live statistics.
- **Evals** (`evals/`): pure metrics, the harness, the local-vs-benchmark comparison, the
  pack behaviour tests, what a labelled set covers (`coverage.py`, over one canonical form of
  a case's input, `canonical.py`), and what a grading run over a hidden evaluation set answers,
  aggregates only (`hidden.py`, `mendari.hidden_result/1`). `method.py` pins the
  [grading method](grading/method-1.0.md)'s version, hash and numbers, for the scoreboard to come.
- **Standards** (`standards/`): FHIR R4 mapping and `Task` output, CDS Hooks discovery and
  cards, the structural R4 check on FHIR inputs (`fhir_structure.py`, from HL7's R4 JSON
  Schema), US Core 6.1.0 checks on them (`us_core.py`, generated from the official packages),
  and the audit trail as AuditEvent and Provenance (`audit_fhir.py`).

## Request lifecycle

1. The input arrives as JSON, a FHIR Bundle, or a CDS Hooks request with prefetch. FHIR is
   mapped onto the pack input by `fhir/mapping.yaml`, and the decision records what the mapping
   looked for and found, as counts (`standards/data_coverage.py`). A part of the record that
   could not be read (a prefetch read that failed, a note that is only a link) makes the
   decision `ABSTAIN` after step 2, before any provider runs.
2. JSON Schema validation. Invalid input becomes `BLOCK`, and no provider sees it.
3. Each provider slot answers its questions. A provider error is recorded per question and
   never raised to the caller. Derived questions are then computed from their members'
   answers, and score answers get their expected value.
4. Policy: rules, then thresholds, then caps.
5. The `DecisionResult` is returned. With a store attached, the record is written and
   `decision.created` is chained (with sign-in on, naming who asked: `requested_by`).
   `HUMAN_REVIEW` and `ABSTAIN` enter the review queue.
6. A reviewer resolves the item (`review.resolved`, with the override flag), and a downstream
   system may report what really happened (`outcome.recorded`), before or after a review:
   the decision keeps both (`final_label`, `observed_label`). Reviews feed the override rate;
   the observed accuracy measures the recommendation against the outcome when there is one,
   else against the reviewer's label.

## Trust boundaries

- **Pack content is code-adjacent.** Regexes, models and policies change behaviour. Packs are
  identified by digest and SHOULD be reviewed like code. v0.1 loads packs only from local
  paths.
- **Packs from a registry are verified before use** (`hub/client.py`):
  - the archive's SHA-256 recorded by the registry;
  - every file's hash in the signed manifest;
  - an Ed25519 signature by a key of the pack's namespace, pinned on first use;
  - the pack digest after safe extraction (regular files only, no traversal, size caps).

  Versions are immutable in the registry, and publishing needs a namespace token (stored
  hashed). A registry can't swap a pack without its publisher's key, and it can't swap the key
  without the user noticing.
- **A registry shows only what it checked**
  ([ADR-0012](decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)): it renders
  each version's card from the digested files, never shows an uploaded card, benchmark or
  leaderboard, and answers `checked` apart from `claimed`. Every registry write is a chained
  event (`mendari registry verify`). The engine shows a pulled pack with the registry's card and
  never takes the benchmark it carries as a reference (monitoring, evaluations).
- **A pulled pack is capped by its origin** (`core/origin.py`, `Decision._origin_holds`): it
  writes where it came from beside itself (`mendari.origin/1`), and served it never automates
  until its organization approves its digest (`registry_approvals`), nor while its registry
  recalls it. A recall is a signed record (`hub/yank.py`); a running server reads each
  registry's recalls in a thread of its own, started by a decision for one of its packs once the
  last read is five minutes old (`server/recalls.py`), and holds a pack whose registry it could
  not read for a day. The Decision Card shows the origin and the approval.
- **Remote providers** (Jev, LLMs) receive only `state_fields` over HTTPS (plain http only
  to a model server on localhost), with credentials read from the environment or the
  deployment's OIDC token. LLM prompts carry the input as JSON data under a system prompt that
  forbids following it, and the response schema is fixed by the pack. Their responses are validated strictly and bounded in size. A batched
  call sends the union of its variants' `state_fields`; a variant that must not share its
  state sets `batch: false`. An owned pack's variant without `state_fields` sends the whole
  input; a pack the deployment does not own (community, registry) must name them, and runs on
  the deployment's model with its `max_tokens`, `timeout_ms` and `reasoning_effort` capped.
  Every variant's config is checked at load against its provider's closed config
  (`providers.config_problems`, published as `docs/spec/provider-configs.schema.json`): such a
  pack is refused for a problem, an owned one warned.
  - With `MENDARI_PROVIDER_ALLOWED_ORIGINS` set (`providers/http.py`), every Jev, LLM and Vercel
    Connect request to an origin it does not list is refused before it is opened
    (`invalid_endpoint`), and an `LLM_BASE_URL` or `JEV_API_URL` off it stops startup. Unset,
    the endpoints are only the deployment's own variables, as before.
- **The server** is open by default and binds to localhost. Production sits behind Vercel
  Authentication, and the docker sandbox publishes ports on 127.0.0.1 only.
  - With an OIDC issuer set (`server/auth.py`), every request passes one guard: a signed-in
    session (PKCE, `id_token` verified with the IdP's JWKS, server-side session, HttpOnly
    SameSite=Lax cookie) or an API key (stored as SHA-256, revocable).
  - The guard maps each route to a permission, a role table grants permissions, and unlisted
    routes need admin.
  - A matrix test pins which routes are public.
  - Every response carries security headers: a CSP with the dashboard's two inline scripts by
    hash (no `unsafe-inline` scripts), `frame-ancestors 'none'`, nosniff, no referrer and
    COOP/COEP/CORP. The threat model is `docs/security/threat-model.md`.
- **Outbound FHIR reads** go through one client (`standards/fhir_client.py`), so SSRF has a
  single guard. That includes CDS Hooks fetching prefetch the EHR left out, and later SMART and
  Bulk.
  - The server's origin must be on `MENDARI_FHIR_ALLOWED_SERVERS`. It is empty by default, so
    CDS Hooks then works from prefetch only, as in v0.1.
  - Plain http is allowed only for loopback and single-label (compose) hosts.
  - Requests stay under the server's base URL, redirects are refused, and responses are capped
    at 5 MB of JSON.
  - A prefetch template's `{{context.x}}` tokens accept FHIR ids only, and the template is a
    read or search relative to the server (`core/spec.py` `PREFETCH`): a pack the deployment does
    not own is refused at load for any other, an owned one warned.
  - Errors never echo the server's body or the token.
- **SMART on FHIR launches** (`standards/smart.py`, `server/smart_routes.py`) use PKCE S256, a
  single-use `state` and a `nonce`.
  - Every endpoint the server's configuration names must be on the allow-list too.
  - The `id_token` is verified with the server's JWKS: RS256 or ES384 only, with `iss`, `aud`,
    `exp` and `nonce` checked.
  - Access tokens stay in the database (`smart_sessions`, keyed by the SHA-256 of the browser's
    opaque id, expiring with the token; not encrypted at rest until the v0.6 PHI mode).
  - The session cookie is HttpOnly, Secure and SameSite=Lax. No refresh tokens are requested.
  - A decision is only made for a resource whose subject is the launch's patient.
- **Bulk exports** (`standards/bulk.py`) go through the same allow-list, and so do the status URL
  and every file URL the server returns.
  - Files are streamed to disk under a 1 GB cap each, and their line counts are checked against
    the manifest.
  - Backend-services assertions are RS384 or ES384, with a 5-minute lifetime and a fresh `jti`.
  - Errors carry OperationOutcome issue codes, never diagnostics.

## Sandbox

`docker compose up` runs HAPI FHIR (R4, in-memory), the engine image, and a worker that seeds
synthetic Patients and Communications tagged `https://mendari.co/fhir/sandbox|synthetic`.
The worker then routes each in-progress Communication through the engine, writes a `Task`, and
marks the Communication completed. Only tagged resources are ever touched.
