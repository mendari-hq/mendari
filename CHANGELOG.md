# Changelog

Notable changes to Mendari, newest first. Each release has a section named by its version
(`## 0.1.0`), written before the release: the release workflow refuses a tag whose version has
no section, or whose section is not the newest. Changes to a pack are in the pack's own
`CHANGELOG.md`.

## 0.1.0

The first release (not yet published).

- **Decision Packs:** the specification (0.2; 0.1 still loads), and the runtime that runs a
  pack's providers (rules, naive Bayes, Jev, LLMs, criteria, CQL, dosing) under a risk policy
  that may only lower automation.
- **Five reference packs**, built and evaluated on synthetic data: Patient Inbox Router,
  Clinical Trial Prescreener, Referral Router, Prior Authorization Preflight and AI Guard. None
  is clinically reviewed or for patient care. No package carries CPT or SNOMED CT codes: where a
  pack needs codes of its own (procedures, a local site's conditions, the code that marks a
  referral) they are fictional, in code systems named as such, and a site maps its own codes.
  So until a site does, the Referral Router matches no real EHR referral through FHIR and the
  Clinical Trial Prescreener reads real diagnoses only through ICD-10-CM (through FHIR, only from
  a Condition's first coding). A check refuses SNOMED CT's url, OID and concept URIs in the
  built wheel and sdist, and any number written as a SNOMED CT concept id in Mendari's own files
  there (`scripts/check_snomed.py`); NOTICE carries NLM's RxNorm acknowledgement.
- **Standards:** a FHIR R4 adapter and CDS Hooks service.
- **Da Vinci PAS: X12 codes are the site's (breaking).** Mendari ships no X12 code value (X12's
  code lists are under X12's terms; the owner's decision of 2026-10-07, LP-14). A PAS request no
  longer fills in its request type and certification type, nor the Coverage's code for self and
  the organizations' entity codes: the request gives them (`request_type` and
  `certification_type` on `POST /api/packs/{pack}/pas`, `--request-type` and
  `--certification-type` on `mendari pas build`), else the settings `MENDARI_PAS_REQUEST_TYPE`,
  `MENDARI_PAS_CERTIFICATION_TYPE` and, for a service type neither the request nor the order
  gives, `MENDARI_PAS_SERVICE_TYPE`. Without one of those three the request is refused (`422`,
  or exit 1) naming its setting. `MENDARI_PAS_SELF_RELATIONSHIP`, `MENDARI_PAS_PAYER_ENTITY`
  and `MENDARI_PAS_REQUESTER_ENTITY` are optional, as in PAS: unset, those codes are left out
  (the Coverage then names the patient as its subscriber). With the values Mendari used to fill
  in set as settings, a request is byte for byte what it was. The sandbox payer answers with
  `MENDARI_SANDBOX_PAYER_REVIEW_ACTION`, or with no review action code when it is unset, and
  its disposition says "Approved". Anyone who relied on the old values sets them.
- **Governance:** a review queue, an append-only hash-chained audit trail on SQLite or Postgres,
  PHI modes (off, hash, encrypt) with key rotation, retention, and an evaluation harness with
  Decision Cards.
- **Evaluation with exact statistics:** a run keeps a per-case ledger (no input text, no case
  id) and its report's result is computed only from it (`mendari.eval/2`), byte for byte the same
  on a second run; each proportion is k of n with its exact Clopper-Pearson interval, each Brier
  score has a seeded bootstrap interval, and an equal-mass calibration error sits beside the
  equal-width one. `mendari compare A B --dataset X` compares two runs case by case (exact
  McNemar, Holm-adjusted). A stored report is chained in the audit trail (`eval.recorded`).
  Cards and the command line show each proportion that way, and a critical label with fewer
  than 100 positives says "insufficient evidence". Leaderboards (`mendari.leaderboard/2`) key
  their rows by pack digest and dataset hash, and one ranking (critical cases first, never
  accuracy alone) orders them everywhere; a row from an earlier pack version is kept, marked
  and unranked. The server serves every share of cases it shows as figures worked out in
  Python (`mendari.figures/1`): beside each report, on the leaderboard (ranked, by set of
  records), the overview's periods, monitoring and the registry's versions; the dashboard and
  the Hub only format them. The card and `mendari eval` show the equal-mass calibration error.
  The dashboard shows each share of cases as k of n with its interval, printed as the card
  prints it, and the leaderboard in the ranking's order with each row's rank or why it has
  none. Its "False automation" is now of the labelled cases, as on the card (it was of the
  automated ones); the subgroups table drops its false-automation column, which the card never
  had; the overview compares a period with the one before by that period's own figure.
  The Hub shows each pack's benchmark accuracy as k of n with its interval, and its leaderboard
  in the ranking's order, one set of records at a time, each row with its rank or why it has
  none (it ranked by accuracy alone).
  Each answer of an `llm` or `vote` provider records the model that answered (the id the
  endpoint reports, when well formed), as Jev's already did: on the decision, the audit
  record, `$decide`, the ledger and the report's `models`. Earlier `llm` and `vote` runs name
  the configured model there.
- **Breaking, for the command line and self-hosted servers: no default LLM model.** An `llm`
  variant asks the model it pins, else the deployment's `LLM_MODEL`; the built-in default (one
  vendor's model) is gone, so `--provider x=llm` without `LLM_MODEL` no longer calls it. With
  neither, the variant is `not_configured` before any call or budget use: a decision that needs
  it goes to a person, never AUTO (HUMAN_REVIEW, or ABSTAIN to the manual fallback when the
  variant answers the primary question), and inside `rules_and_llm` the keyword rules still
  answer. An empty `LLM_MODEL` counts as unset, and `/api/health` reports
  `llm.default_model` as null. `mendari calibrate` refuses in one line before it prints or asks
  anything, and `mendari agreement` refuses when a variant it compares, or one of a composite's
  members, cannot be called (the other member alone would have made two variants look equal).
  The inbox pack is 0.4.2: a manifest comment named the default, so its digest changes.
- **Dashboard** and REST API; sign-in through an organization's OpenID Connect provider; API
  keys by role. With sign-in on, the engine refuses to start under PHI mode `off`, set or left
  unset, everywhere (it warned outside Vercel): `off` is for a local sandbox without sign-in.
  The compose sandbox's signed-in engine (profile `signin`) runs `encrypt` with two keys you
  generate once into `.env` (README); an EHR launch without sign-in still starts under `off`
  written out, with a warning.
- **Hub:** a pack registry and its client (`mendari pull`, `mendari search`), and community
  packs. A site can share an evaluation of a composite provider (`any_of`, `cascade`, `vote`):
  its model lists each member's and a failed member's error names the member, as the
  evaluation records them, and the registry refuses member names that are not the pack's own
  (it refused any report from a composite). A version published before this records no
  members, so it refuses such a report with the reason (publish a new version). A shared
  report's error codes must be Mendari's own, a closed set the spec lists (they were checked
  only as names), and the type checker holds every provider error to that set. A pack from a
  registry with a reference pack's id and version but other files is refused at publish, pull
  and load (it would have been served in the reference pack's place); one cached before is
  listed under `/api/health` `invalid`, with the directory to remove.
- **Input schemas bounded in shape:** the input schema of a pack the deployment owns nests at
  most 32 deep, chains at most 16 `$ref`s, applies at most 1,000 schema objects to one value,
  refers back to itself only as a recursive list or tree does, and holds no
  `unevaluatedProperties` or `unevaluatedItems` below another; one beyond these is refused at
  load. Every shipped pack is well within them (6 deep, 1 `$ref`, 2 schema objects).
- **Provider configs checked at load:** every variant and composite member, the ones a pack does
  not use by default too, is checked against its provider type's closed config (any other key,
  a value out of range, a pack file it cannot run with), published as a JSON Schema
  (`docs/spec/provider-configs.schema.json`). A community or registry pack with a problem is
  refused at load; any other pack loads and says so (`mendari list`, the start-up log,
  `/api/health` `pack_warnings`), and `mendari validate` refuses both. For a community or
  registry pack, every model variant names the input fields it sends (`state_fields`), the
  deployment's model is used, `max_tokens` and `timeout_ms` are held to the provider defaults
  (a lower value stands) and a `reasoning_effort` above `low` is dropped: a registry pack that
  pinned an LLM model now runs on the deployment's (no shipped pack pins one).
- **Provider allow-list:** `MENDARI_PROVIDER_ALLOWED_ORIGINS` (comma-separated origins, off
  unless set) names the only origins Jev, LLM and Vercel Connect requests may reach: any other,
  the AI Gateway's default included, is refused before it is sent (`invalid_endpoint`, so the
  decision goes to a person), an `LLM_BASE_URL` or `JEV_API_URL` off the list stops the server at
  start-up, and signed-in `/api/health` shows it (`provider_allowed_origins`). Unset, nothing
  changes.
- **Prefetch and wording templates checked at load:** a CDS Hooks prefetch template is a read or
  search relative to the EHR's FHIR server (`Type/<id>`, `Type/{{context.<name>}}`,
  `Type?<query>`), and a card summary's or FHIR Task text's placeholders are bare names (no
  conversion, format spec, attribute or index). A community or registry pack with one that is
  not is refused at load, its Task text included; any other pack loads warned of its prefetch
  and card summaries, its Task text renders as before, and `mendari validate` refuses both. Every
  shipped template passes, and no pack renders different wording.
- **Distribution:** a wheel and an sdist, container images (the engine with every pack, and one
  per pack), and deployment templates for docker compose and Kubernetes. Each image carries its
  licences in `/licenses`: Mendari's LICENSE and NOTICE, `THIRD_PARTY_NOTICES.md` written from
  what the image holds (Debian's packages, whose copyright files stay, every Python package, the
  libraries psycopg-binary bundles, what packages link statically and the Rust crates compiled
  into them, each with where its source is), the texts the image would otherwise lack, and
  `SOURCE.md`; its label names Apache-2.0 as Mendari's licence only. A release attaches an SBOM
  of each finished image, scans its licences, publishes each image's corresponding source
  beside it (`<tag>-sources`, with the SBOM and every package's source location), and pushes no
  image that fails; CI runs the same SBOM scan on every change. The image still builds with
  Docker's classic builder.
- **The command line:** every error is one line per problem and exit 1, never a traceback
  (`--debug` or `MENDARI_DEBUG=1` shows one); a usage error is one line too, exit 1 (argparse's
  was 2); `--version`. `mendari test` refuses a behaviour test whose `expect` is missing,
  empty, misspelt or names what the pack can never decide, and fails when a pack has no tests;
  `mendari validate` reads every declared dataset; a typed provider setting that applies to
  nothing in the pack is an error. `audit verify`, `audit export`, `feedback export` and
  `monitor` open the database read-only and refuse one that does not exist; `phi purge`,
  `phi erase` and `notes erase` refuse one too.
