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
- **Da Vinci DTR: errors as DTR 2.2.0 asks (breaking for a client that read a 200).**
  `POST /fhir/Questionnaire/$questionnaire-package` warns (`warning`, `not-found`) in `outcome`
  of every questionnaire asked for that is not found here, beside any package (oper-9); it was
  an information note, and only when no package was built. A canonical of Mendari's form whose
  names are not the pack's labels is not found too: it no longer gets a questionnaire made up
  for it. Nor is one with a version other than the pack's: it got the current version before.
  A questionnaire that is found comes beside the orders' packages, the union DTR asks for, each
  questionnaire version once; it was dropped whenever an order had a package. An order or a
  `context` that names no decision of the caller's organization is a warning too, which points
  at the parameter and names no id; it was silent. With nothing to return, they are a `422`
  OperationOutcome (`not-found`, spec-130), not a `200`; an unknown id and another
  organization's get the same answer, which repeats neither. A request that names no order,
  context or questionnaire is a `400` (oper-6). A decided order that needs no documentation
  keeps its information note (oper-8), and a package that was built is as before.
  `scripts/davinci_sample.py` writes a tenth sample, a package with both warnings, for
  `fhir-conformance`. "Validated against DTR 2.2.0" now reads profile-validated (HL7 validator,
  offline): that checks the answers' structure, not how the operation behaves.
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
- **Hidden evaluation sets in the dashboard: the Hidden sets screen** (LP-11n, `#evalsets`).
  An administrator who signed in, in memberships mode where hidden sets are on (their keys'
  ids in `/api/health`), finds **Hidden sets** in the section list; labellers, other roles,
  claims mode and a deployment without the keys never see it or its link, and their pages ask
  for no set. It lists the organization's sets (name, id, pack, state, cases) and opens one:
  its facts (state, commitment, evaluation runs used and left, until when it is in use) and
  tabs for its progress (cases by status, each labeller's first and third labels, named from
  the members list), drawing its cases (per stratum or in all, seeded), giving them to two
  labellers and a disputed one to a third (only labellers who manage no set are offered), its
  rater agreement (percent agreement as k of n with its exact interval, each pair's kappa and
  the pool's alpha with their intervals, "not estimable" in words, never a blank) and its
  coverage (counts by label, critical labels with the insufficient-evidence mark, the mix of
  languages, sources and strata). The last two show what the key holder stored, else "Not
  measured yet". A frozen, in-use or retired set is followed, never drawn or assigned. Ids,
  counts, names and aggregates only: never a case, an input, a label given, an attribute, a
  case id or a case hash. Runbook: `docs/operations/evalsets.md`, Curating from the dashboard
  API.
- **Hidden evaluation sets, graded by the organization's grading key** (LP-11, mounted with both
  evaluation-set keys in either account mode). `POST /api/evalsets/{id}/runs` with
  `{"pack": ..., "variants": {...}}` runs the pack a frozen or active set was made for over its
  cases, in the server's process, with local providers only (a slot the deployment runs remotely
  needs a local variant named; a remote one is refused before any case is read), and answers
  aggregates only: `mendari.hidden_result/1` (the set's id and commitment, how many cases, this
  run's number and the runs left, the pack and providers, each question's metrics with
  calibration bins and Brier intervals over 10 cases or more only, the outcome counts and each
  critical label's counts, the threshold sweep and error counts; never a case, its id or hash, a
  missed or misclassified case, a subgroup or the dataset's identity). Each run is chained before
  its cases are read (`evalset.graded`, and `evalset.activated` on a frozen set's first) and
  counted against the set's rotation rule; nothing is stored (no decision, report, ledger or
  usage; `/api/evals` does not change). Only the organization's grading key passes (`grade`:
  never a person, an administrator, another key, a token or open mode); another organization's
  set is a `404` like an unknown one. Every refusal and failure is answered with a fixed `reason`
  and sentence, never a message: a set changed since its freeze is a `409` `set_changed`, and the
  server's log names the set only. Its rate-limit class is `evaluate`; security-log event
  `evalset.graded`, naming the set's id only; the `Grading` OpenAPI tag and the `/docs` `grade`
  row. Runbook: `docs/operations/evalsets.md`, Grading a hidden set; spec:
  `docs/spec/evaluation-reports.md`.
- **Hidden evaluation sets, managed by the organization's administrators** (LP-11, mounted as a
  labeller's routes are: memberships mode with both evaluation-set keys only).
  `GET /api/evalsets` lists the organization's sets (id, name, pack, state, when made, cases);
  `GET /api/evalsets/{id}` reads one: its name, pack and questions, state, commitment, rotation
  rule and runs used and left, its cases by status and each labeller's assignments by user id,
  and the coverage and rater agreement the key holder last stored, with when they were measured
  (null before `mendari evalset agreement` or `coverage`, or freezing; each rater pair's kappa
  without the pair's label counts, which the key holder's commands keep);
  `POST /api/evalsets/{id}/sample`, `/assignments` and `/adjudication` draw, assign and
  adjudicate as the commands do (chained alike), answering counts only. Only a person holding
  `curate` (an administrator who signed in) passes: never a key, a token, a labeller or open
  mode; another organization's set, an unknown one and a malformed id are one `404` naming no
  id; no administrator is ever assigned, the caller included. No route measures, freezes or
  retires, and the answers are closed models that carry no input, label given, attribute or
  case id. Not gated by the notice: they take no data in and mint no credential. Like an
  administrator's other writes, they have no rate-limit class. Security-log events
  `evalset.sampled`, `evalset.assigned` and `evalset.adjudication.assigned`, naming the set's id
  only. Runbook: `docs/operations/evalsets.md`, Curating from the dashboard API.
- **Hidden evaluation sets, labelled through a labeller's own routes** (LP-11, memberships mode
  with both evaluation-set keys only: the hosted site, in claims mode without them, mounts none).
  `GET /api/labelling` gives a labeller their own counts, set by set; `GET /api/labelling/next`
  their oldest open case, its input and the set's questions with their labels and nothing else,
  the read chained (`evalset.case_read`) before it is sent, or `204`; `POST /api/labelling/{id}`
  takes `{"labels": {...}}` and answers `{"remaining"}`, never how the label compares. Only a
  person holding `label` (a labeller's membership) passes: never a key, a token, an administrator
  or open mode, and whoever also manages the organization's sets is served nothing. An unknown,
  another labeller's, another organization's, withdrawn or answered assignment, or one in a set
  no longer a draft, is one `404`; a label missing a question or with a word not among its labels
  is a `422` naming the question, never the value. A new rate-limit class, `label` (60/min, burst
  30, per labeller: `MENDARI_RATE_LIMITS=on,label=…`); not gated by the notice; security-log
  events `evalset.case.read` and `evalset.labelled`, naming the set's id only. Runbook:
  `docs/operations/evalsets.md`, How a labeller labels.
- **Hidden evaluation sets, frozen, graded in the process and retired** (LP-11, the key holder's
  command line and the store; graded through the grading route, above): every write
  refuses a set that is no longer a draft, in its own exclusive transaction, writing and chaining
  nothing. `mendari evalset freeze <set> --max-runs N --max-age-days D [--drop-unsampled]
  [--pack P]` freezes a set whose drawn cases are all agreed or resolved (an undrawn one is refused,
  or removed and chained with `--drop-unsampled`; both limits of the rotation rule are required,
  with no default): its content (`mendari.evalset/1`: the header, the rule and when it froze, and
  every case by id with its input, final and derived labels, status and attributes) is hashed, the
  salt, the hash and the derived labels sealed as its freeze record, the final labels' coverage and
  agreement as its aggregates, and only the salted commitment chained (`evalset.frozen`); it prints
  the commitment and each critical label's distinct positives, warning under 100. In the store, the
  grading read (for the grading route, LP-11l) serves a live grading key of the set's organization
  while the rotation rule holds (fewer chained reads than `--max-runs`, younger than
  `--max-age-days`), recomputes the content against the commitment (refusing, naming no case, when
  it changed), activates a frozen set (`evalset.activated`) and chains the read (`evalset.graded`:
  the grading key, the pack's digest, the run's number) in one exclusive transaction before its run
  receives the cases in memory; only the run's answer comes back, nothing is stored.
  `mendari evalset retire` checks the content once more and chains `evalset.retired`, publishing
  nothing; `mendari evalset show` reads a set's state, commitment, runs used and left and its stored
  aggregates (no key needed but for those). `mendari audit verify --evalsets` and `rotate --check`
  recompute every set the chain froze against its commitment, whatever its row's state now, and
  `verify` checks each set's state against its events (frozen, activated and retired once, no
  draft's write after the freeze, no read after the retirement; counted `frozen`); a retired set's
  state edited back grades nothing, since the read checks the chain's retirement. Runbook:
  `docs/operations/evalsets.md`, Freezing a set.
- **Hidden evaluation sets, labelled blind** (LP-11, the key holder's command line and the
  store; a labeller labels through their own routes, above): `mendari evalset sample`
  draws a draft set's cases, seeded, within each stratum (`--per-stratum N` or `--cases N`),
  each drawn case's weight (N_h / n_h) sealed beside its attributes; `mendari evalset assign`
  gives each drawn case two different labellers, balanced across labellers and pairs, and
  `adjudicate` each disputed case a third who is neither of its first two, each refusing anyone
  without the `labeller` role in the set's organization and any administrator there (separation
  of duties), and moving an open assignment whose membership ended to another labeller;
  `mendari evalset progress` counts cases by status and each labeller's assignments (no case
  named, no key needed); `mendari evalset agreement [--seed] [--resamples] [--json]` and
  `coverage [--json]` measure the set (`mendari.agreement/1` over the first-pass labels,
  `mendari.coverage/1` over the settled ones) and store both sealed. In the store, a labeller is
  shown a case's input and the set's questions only, and their label is sealed, bound to its own
  id, one per assignment and never changed; two first-pass labels settle a case `agreed` or
  `disputed`, an adjudicator's `resolved` (a majority on every question) or `unresolved` (a
  three-way split). New tables `evalset_assignments` and `evalset_labels`; new events, each of
  ids, keyed case hashes, user ids, counts and salted commitments, never a label
  (`evalset.sampled`, `evalset.assigned`, `evalset.case_read`, `evalset.labelled`,
  `evalset.measured`); `mendari audit verify --evalsets` now checks every assignment and
  recomputes every label's commitment, and rotation, its check and the export cover labels.
  Removing a case removes its assignments and labels. Runbook: `docs/operations/evalsets.md`,
  Labelling a set.
- **Hidden evaluation sets' keys rotate** (LP-11, the key holder's command line):
  `mendari evalset rotate --to <id>` re-seals every set's sealed values under the current
  evaluation-set key in batches, each chained (`evalset.rotated`: key ids and counts),
  compare-and-set, in every state of a set; an interrupted run resumes, a value no key opens is
  listed by its set and case hash, and a key it moved values off seals nothing more.
  `rotate --check` proves, reading only, that everything opens with the current key alone, each
  case's commitment and keyed hash match, and its index rows under the current index key are
  there. `mendari evalset reindex` rebuilds the hidden-case index under a new index key in one
  transaction (`evalset_index.rebuilt`); an index key it replaced indexes nothing more, and it
  never goes back to an older one.
  `mendari evalset export` writes the sealed sets and cases as stored (ciphertext, ids and key
  ids) to a new owner-only file, needing no key and chaining nothing. `audit verify --evalsets`
  now opens every sealed column a set or case has, the freeze record and aggregates included
  once a freeze fills them. A test pins each column of the `evalset_` tables as sealed or clear.
  Runbook: `docs/operations/evalsets.md`, Rotating the evaluation-set key.
- **Hidden evaluation sets, stored** (LP-11, the key holder's command line; no route yet):
  `mendari evalset create`, `add`, `remove-case` and `list` keep an organization's sets of a
  pack in the deployment's own database (`evalset_sets`, `evalset_cases`, `evalset_index`), never
  in the repository. Cases are hand-written: another source, a template, a `system` or
  `feedback` block, a missing author, language or stratum, and an input equal (exactly or under
  `norm/v1`) to a public case (the bundled reference data and datasets, the generators' default
  output) are refused, naming the line only, and a file inside the package or a checkout is
  refused. Inputs, attributes and headers are sealed; the chain holds ids, keyed case hashes and
  salted commitments (`evalset.created`, `evalset.case_added`, `evalset.case_removed`, and
  `evalset_index.epoch` for a new index key). `mendari audit verify --evalsets` checks every
  sealed case against them, and that each set's header opens (read-only; without the keys,
  rows are counted unchecked). A stray, retired or PHI key, or a key the database knows in the
  other role (sealing or index), writes nothing, and a server with the keys checks them at
  startup; a PHI key or input hash key the database knows as an evaluation-set key stops the
  server and is never marked a PHI key. PHI retention, erasure and rotation never touch the
  sets.
- **Hidden evaluation sets' permissions** (LP-11; no route needs them yet): `label`, for a person
  holding the `labeller` role on a membership (memberships mode only; one holding no other role
  sees a page saying so and nothing else); `curate`, for administrators who signed in, who no longer hold everything
  (neither labelling nor grading); and `grade`, held only by an organization's grading key,
  which only the operator makes (`mendari access grader-key`, at most 365 days, chained) and an
  administrator may revoke but not rotate. Open mode holds none of the three.
- **Hidden evaluation sets' keys** (LP-11, off unless set): `MENDARI_EVALSET_KEY` seals a set's
  content (AES-256-GCM, each token naming its key and bound to its kind, set, case and label),
  `MENDARI_EVALSET_INDEX_KEY` keys the hidden-case index and the chained case hashes, and
  `MENDARI_EVALSET_READ_KEYS` opens during a rotation. A malformed, half-set or reused key, one
  equal to any other key of the deployment, stops the server; with the keys, it needs sign-in
  and, on Vercel, Production. Health shows their ids to those who may read. Nothing is stored
  with them yet.
- **Coverage of a labelled set:** `mendari data coverage PACK --dataset X` (`mendari.coverage/1`)
  counts a set's distinct cases (two inputs that differ only in case, spacing, punctuation and
  symbols, invisible characters, Unicode form or a whole number's spelling are one case,
  `norm/v1`), the distinct positives of each critical label, with a warning under 100,
  duplicates whose labels disagree, and the language, source and stratum mix.
  It prints counts, never a case's text. Its two canonical forms of an input are pinned, as the
  hidden evaluation sets (LP-11) and the pack store's upload check (LP-20) will use them.
- **Rater agreement** (`mendari.agreement/1`, for LP-11's blind labelling): percent agreement as
  k of n with its exact interval, Cohen's kappa for each pair of raters and Krippendorff's alpha
  across the pool, exact, each with a seeded percentile bootstrap interval; an undefined
  coefficient or interval says why. Checked against hand-computed references, statsmodels and
  scipy; no dependency is added.
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
- **Lifting an automation hold asks first.** The Decision Card's Lift opens the confirmation
  dialog every other change that takes a safeguard away uses, saying the pack then decides on
  its own again (another hold, a recall, an approval it still needs and its risk level's limits
  keep applying); Cancel keeps the hold. Holding stays one click.
- **The Access view's Activity tab shows the newest events.** `GET /api/audit/administrative`
  takes `order=desc`, newest first (`next` is the following page's `after`, in the same order),
  on SQLite and Postgres; the default order, its cursor and the FHIR export are unchanged. The
  tab asks newest first, so a 90-day window past its 5,000-event cap shows its newest events,
  not its oldest, and its note and caption say which.
- **A read with a session cookie and no Fetch Metadata needs Mendari's own header (security).**
  A browser without Fetch Metadata (Safari before 16.4) following another site's link sends
  neither `Sec-Fetch-Site` nor `Origin`, so the origin check read it as a script, and the link
  read an API route in the person's name. Such a read (a Mendari cookie, GET or HEAD, no
  `Authorization`, neither header) now reaches more than the public routes and pages only with
  `X-Mendari-Request`, which Mendari's own pages send on every request and another site cannot
  add without a CORS preflight; without it, a `403` logged as `auth.origin.refused`
  (`no_fetch_metadata`). The dashboard's downloads (the wrong cases' JSONL in claims mode too)
  and the links to the OpenAPI document, on the landing and in the API reference, are fetched,
  not followed, and the API reference's own requests send it. Scripts that read with a session
  cookie send `X-Mendari-Request: 1` (the pilot check, the sandbox's EHR launch and the ZAP scan
  do), as the landing and the API reference's introduction now say. Requests with Fetch
  Metadata, an `Authorization` header or no Mendari cookie, and unsafe methods, are checked as
  before.
- **The community check's PHI warnings read two more shapes, beside their word.** Ten digits run
  together (`6175552368`, or with `+1`) warn as a phone number next to a word such as phone,
  call, cell, fax or contact, in its forms ("contacted", "faxed"); a date in words ("January 2,
  1980", "Jan. 2 1980", "2 January 1980") warns as a date of birth next to born, DOB, D.O.B.,
  date of birth, birthday or birthdate. Without that word a record-number-like run of digits
  and an ordinary date stay unflagged. A word counts only whole, for the numeric dates' check
  too: the "cell" of "cellulitis" or the "born" of "newborn" at the edge of the 40 characters
  read beside a number or a date no longer counts. Warnings still never quote the text.
- **A downstream outcome no longer overwrites a reviewer's label.** `POST
  /api/audit/{decision_id}/outcome` kept what really happened in the decision's `final_label`,
  over a reviewer's when the decision had been reviewed, while `human_override`, `reviewer` and
  `resolved_at` still described the reviewer's choice. The outcome is now the decision's
  `observed_label` (a later one replaces it; the chain keeps every one), and the review's
  columns never change after the review. Observed accuracy (`/api/stats`, monitoring) measures
  the recommendation against the outcome when there is one, else against the reviewer's label.
  The dashboard's audit trail shows both ("nurse · observed physician"); the FHIR Task already
  carried both from the chain. A database from before this release gains the column at the
  next start, and its rows are put right from the chain (the reviewer's label back, the latest
  outcome's moved).
- **Fixes from the bug audit of 2026-10-15.** A provider or adapter failure that escaped as a
  500 with no audit record is now a refusal the caller can read: a dose "every 0 hours", a
  non-ASCII `base64Binary` attachment, an allow-list check on a URL whose port is not a number,
  a CDS Hooks request whose `context`, `prefetch` or `fhirAuthorization` is not an object, a
  Jev model list that is not JSON, a token lifetime of `Infinity`, an identity provider whose
  `issuer` is not a string; a CQL definition the engine never evaluated is an invalid response,
  not a confident `unknown`; a vote weight must be finite. Server: a negative `limit` on
  `/api/audit`, `/api/review`, `/api/feedback` and `/api/deployments` is 0, never "no limit"
  (SQLite) or a 500 (Postgres); a scoped key or token gets the public `/api/health` only; `HEAD`
  needs what `GET` needs; an authorization endpoint with a query of its own is extended, not
  given a second `?`; in claims mode a new sign-in ends the browser's earlier session, as in
  memberships mode. The eval gate fails a change that stops measuring a critical label `main`
  measured, or whose primary accuracy can no longer be measured. JSON Lines readers split at
  `\n` only (a U+2028 inside a case's text is not a line end). A daily limit of 0 counts
  nothing. The community check scans the files the manifest names as datasets or tests for PHI
  lookalikes (not only `data/` and `tests/`), and spots `(212)555-1234` and `123 45 6789`. The
  card lists a boolean property schema. Dashboard: a slow record reply never replaces a newer
  drawer or a changed outcome filter; a pack whose details fail to load shows the error view
  with Try again instead of the previous pack's data; an evaluation run that finishes after
  the tab is left no longer throws; a numeric enum reads back as a number.
- **The command line:** every error is one line per problem and exit 1, never a traceback
  (`--debug` or `MENDARI_DEBUG=1` shows one); a usage error is one line too, exit 1 (argparse's
  was 2); `--version`. `mendari test` refuses a behaviour test whose `expect` is missing,
  empty, misspelt or names what the pack can never decide, and fails when a pack has no tests;
  `mendari validate` reads every declared dataset; a typed provider setting that applies to
  nothing in the pack is an error. `audit verify`, `audit export`, `feedback export` and
  `monitor` open the database read-only and refuse one that does not exist; `phi purge`,
  `phi erase` and `notes erase` refuse one too.
