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
- **EHR pilot readiness, the baseline** (LP-23a; nothing changes how a pack decides).
  `notes/ehr-scorecard.md` grades readiness for a pilot with one EHR on rubric v1: seven
  dimensions, each at a level from 1 to 5 by named criteria, each criterion with the test that
  shows it or "missing"; the overall grade is the lowest dimension's (level 1, graded at
  `06d5332f` with LP-23a), and every grade says its scope (synthetic data and test keys only, not
  a vendor certification). Failure behaviour is at level 1 under the default FHIR validation
  mode, `warn`: input that is R4 JSON but breaks R4's other rules is decided with its issues
  recorded, and only `enforce` blocks it.
  ADR-0014 (Proposed) records how an EHR is to call the CDS Hooks services with its own signed
  token, and `notes/epic-sandbox-checklist.md` what the owner registers in Epic's sandbox and
  notes back. The sandbox now builds synthetic US Core FHIR for the trial and prior-authorization
  packs too (`trial_entries`, `prior_auth_entries`: the same entries on every run, no SNOMED CT,
  deciding as their records do; an order the prior-auth pack has no requirements for is refused
  through FHIR, as before), and `inbox_entries` takes the time its message is sent. A
  golden pins how the four packs with a FHIR mapping decide each of their synthetic FHIR cases
  today, input hash included (`scripts/fhir_decisions_golden.py`,
  `tests/fixtures/fhir/decisions-golden.json`), for the LP-23 steps that change the FHIR path.
- **An EHR calls CDS Hooks with its own signed token** (LP-23b, ADR-0014). Register the EHR in
  `MENDARI_CDS_CLIENTS`, a JSON list checked at startup: its `iss`, organization, the FHIR bases
  it may send as `fhirServer` and its public keys, by value or from one `jwks_uri` on
  `MENDARI_FHIR_ALLOWED_SERVERS` (kept an hour, fetched again at most once a minute, by each
  instance, for a `kid` it does not know). `MENDARI_PUBLIC_URL` must be set. A token is verified
  in full (RS384 or ES384; a header of `alg`, `kid`, `typ` and `jku` only, `jku` the registered
  URL exactly; `aud` the public URL plus the path called; `exp`, `iat` and a `jti` used once, at
  most 330 seconds; a pinned `sub` and `tenant`), and becomes the principal `cds:<id>` of the registered
  organization, which lists and calls the CDS services and reaches nothing else, sends only its
  registered `fhirServer` (else `403`, before anything is fetched), and is rate limited and
  capped like any principal. Its decisions are chained as asked for by `cds <id>`. Every token
  that does not verify is one `401`, its reason in the security log (`cds_<reason>`), and its
  `jti` is claimed only after every check, the organization's record and the route's permission
  included. **A change for open mode:** a JWT on `/cds-services` is now verified there too, and
  refused without a registry, where before it was never read and a forged one got cards; a call
  with no credential is answered as before, and off `/cds-services` open mode still reads no
  credential. `AuditStore.smart_claim` now deletes up to 100 expired session-table rows, as each
  put does, and the session table keys any string, a lone surrogate in a `jti` included (every
  key made before is unchanged). Two fixes found on the way, each a `500` before: a JWK Set whose
  `kty` or `key_ops` is of the wrong JSON type is refused as any bad key set is (a `422` when a
  Backend Services client is registered), and a FHIR server's answer nested too deep to read is
  refused as invalid JSON. See [Connecting an EHR](docs/guides/connecting-an-ehr.md).
- **A hook that always answers (LP-23d; two changes in behaviour).** A CDS Hooks service reads
  each prefetch key on its own. When a key cannot be read (a failed read with its reason:
  `network`, `timeout`, `http_status`, `unauthorized`, `not_json`, `too_large`, `off_server`, a
  search past the page cap `cap`; an OperationOutcome sent or read, `operation_outcome`; a
  resource or search page Mendari read that is not R4 JSON, `not_r4`, or not the type asked
  for, `unsupported_type`), the decision is ABSTAIN before any provider runs, its reasons
  `could not read <key>: <reason>`, never a URL, a query or what a server answered. The hook
  answers `200` instead of the `422` it gave before (or the `500`, for a search page whose
  `entry` or `link` was not R4's shape), the decision is recorded like any other, and its card is
  an `info` card naming the keys, whatever the pack's `show` says. Refused input still BLOCKs, a
  request fault stays a `4xx`, the structural R4 check on every value the client sent (an
  OperationOutcome among them) stays a `400`, and a `fhirServer` off the allow-list is still
  refused with nothing fetched or recorded. A key whose value is null is "no data" (CDS Hooks
  2.0) and is no longer fetched; a key sent empty (`{}`, `[]`, `""`) still counts as left out,
  as before. A value that is none of these and not a resource (a string, a number, a list, an
  object with no `resourceType`) is unreadable (`unsupported_type`): it used to be read as no
  data, or for a list as the resources it held, and a required resource it stands in for is
  still refused (`422`), as before. For Da Vinci CRD, an order that abstained this way gets
  `covered: indeterminate` for a `technical` reason, with fixed text, when its Coverage was
  read; without the Coverage there is no system action. The SMART launch's decide reads its
  record the same way (a `401` still ends the launch, and a focus resource that cannot be read is
  an error, as before). Every FHIR decision now carries `data_coverage`
  (`mendari.data-coverage/1`) in its chained record (`GET /api/audit/{id}`), and in the answer of
  `fhir/decide` and of the SMART launch's decide; a CDS Hooks card and `$decide`'s
  GuidanceResponse carry the decision id that finds it (`extension.mendari.decision_id`, the
  `identifier`), not the record itself. For each mapped field and list it holds the paths looked
  for, the code systems the pack declares, the pack's own codes, and what was found, as counts,
  with what could not be read counted by reason. It never holds a value, an id, text, a
  patient's code or a system the pack does not declare. A decision on JSON input carries `null`,
  and older records still verify.
  - **Changed: decisions on notes Mendari cannot read move from a possible AUTO to ABSTAIN.** A
    `base64_text` attachment that is only a `url`, or whose data does not decode to text, used
    to read as no notes, so the decision was made as if the record held none, and could
    automate. It now abstains (`not_fetched`, `undecodable`) in every pack and on every FHIR
    route (CDS Hooks, `fhir/decide`, `$decide`, the SMART launch, `mendari decide --fhir`).
    Readable input decides byte for byte as before (the 23a golden is unchanged).
  - **Changed: a CDS Hooks request with neither the prefetch nor a `fhirServer` answers `412`**
    (`PrefetchRequired`), as CDS Hooks 2.0 says. It answered `422`. A key sent empty counts as
    not sent here too, as it did.
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
- **Operations: a test alert and the status checks for a provider.** `mendari alerts test`
  posts one test alert (`alerts.test`, with no alerts in it) to `MENDARI_ALERT_WEBHOOK` through
  the same delivery as every alert, in its format and signed with its secret, tried once and
  recorded nowhere; it prints whether it was delivered and the webhook's answer, never the URL
  or the secret, and refuses without a webhook and in CI (`CI` or `GITHUB_ACTIONS` set).
  `scripts/status_checks.py --export json` prints the checks in `deploy/status/checks.yaml`
  for a status provider instead of running them: each check's absolute URL, method, JSON body,
  interval and expected statuses, redirects not followed, the host the sign-in redirect must
  leave and the path it must not have (Mendari's own `/auth/login`), the decide check's
  `outcome` as a body keyword, and a header that names the key's environment variable, never
  its value. A key the export does not know stops it. The script's own sign-in check now
  compares the redirect's host alone, as the export does: a redirect to the same host on
  another scheme fails.
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
  The Hub shows no benchmark or leaderboard a publisher uploads (LP-18, below), only the
  registry's own regression check, as k of n with its interval.
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
- **Hidden evaluation sets: a draft's statuses and weights, and every set's stored aggregates,
  covered by commitments** (LP-11p, what 11o's review found nothing checked). Each draw
  (`mendari evalset sample`, `POST /api/evalsets/{set}/sample`) chains a commitment to every
  drawn case's weight by its keyed hash, each with a salt new at each draw (`weight_refs` on
  `evalset.sampled`), and takes as drawn before what the latest draw committed to, never a
  case's status. Each store of a set's aggregates (`evalset agreement`, `coverage` and `freeze`)
  chains a commitment to them, with their salt sealed beside them (`aggregates_ref` on
  `evalset.measured` and `evalset.frozen`). A case's status, still in the clear, is checked
  against its labels and the set's latest draw; each label counts in the round and for the
  labeller its `evalset.labelled` chained, never as an assignment row says now, every label the
  chain recorded must be there, and an adjudicator's label settles only a case whose two
  first-pass labels differ. So a status edited by SQL (a disputed case made `agreed`, which
  freezing used to commit to with its first labeller's answer), a label deleted or an
  assignment moved to another round, a weight re-sealed or put back from before a draw, and
  aggregates re-sealed, put back or removed now break `mendari audit verify --evalsets` and
  fail `mendari evalset rotate --check`; `freeze`, `agreement` and `coverage` refuse such a set,
  counting its cases and naming none (a draw or a label landing while they read says "run it
  again" instead); `assign` and `adjudicate` (and their curators' routes, a `400`), which still
  open nothing, refuse a status the chain's draw and labels do not allow, so no case goes to a
  labeller on it; and `mendari evalset show` and `GET /api/evalsets/{set}` refuse such
  aggregates (a `400`), naming the set only. An agreed case's status edited to `disputed` still
  reaches an adjudicator (telling it from a disputed one takes the labels' values), but that
  label no longer settles it, so the edit stays found. Sets made before this release verify as
  before: both checks count the rows no commitment covers yet (`predating`, a new count in their
  answers, never an id), still check those sets' statuses against their labels, and take their
  weights, whether an unlabelled case was drawn, and their aggregates as they are until the set
  is drawn or measured again (a set frozen before it keeps its aggregates unchecked: take its
  gate's positives from `freeze`'s output). A draft's header stays uncovered until it freezes.
  The threat model, ADR-0011 and the runbook say what is covered and what is not.
- **Hidden evaluation sets, end to end** (LP-11 built, 11a to 11o; off until the owner sets both
  keys). One test takes a set through its whole lifecycle in the runbook's order, on SQLite and
  Postgres (`tests/test_evalset_end_to_end.py`): two organizations, a set made and filled from
  the command line, drawn, assigned and adjudicated through the administrator's routes, labelled
  blind through the labellers', followed and measured from the command line, frozen, graded
  through the grading route with a grading key and a body as the runbook writes them, shown and
  retired; then `mendari audit verify --evalsets` passes, `mendari evalset rotate --check` is
  ready and the set's chain holds its events in the lifecycle's order, and no answer, log line
  or command's output held a case's content but each labeller's own next case its input. The
  launch gates' `labelling-underway` and `hidden-sets-frozen` name the tests that hold them,
  each still waiting for the owner's attestation. The threat model names hidden sets' residual
  risks: the key holder can decrypt offline; an administrator can link one case's events by its
  keyed hash and see who labelled it, and the chain keeps labellers' user ids; adjudication shows
  which cases were disputed; until a set freezes, no commitment covers its header, its cases'
  statuses or their weights, so freezing commits to them as they are, and none covers a set's
  stored aggregates (LP-11p, above, covers all but the header); and paraphrased public cases
  pass. The runbook
  (`docs/operations/evalsets.md`) gains the owner's steps (the two keys, why Production only, a
  lost key, the grading key's life and expiry, each set's rotation numbers, the two gates'
  attestations) and a set from start to end, and now says that every writing `mendari evalset`
  command refuses a database no server used and that a grading key lives 90 days unless
  `--days` says otherwise. ADR-0011 records what was built and its consequences.
- **Statistics for the scoreboard** (LP-17b, `mendari.evals.stats`). Three additions, which no
  format uses yet, so every stored result is unchanged: a stratified bootstrap (each case drawn
  from its own stratum, so every stratum keeps its size; one stratum is the plain bootstrap),
  for macro-F1 within each true label; a weighted share for cases drawn with unequal
  probabilities (the Hájek estimate, Kish's effective n and the Korn-Graubard interval:
  Clopper-Pearson at the effective counts, unrounded, so no hits and all hits get Korn-Graubard's
  bounds too; a bound is 0 or 1 only where its count is under one, and one weight for every case
  gives the exact interval of k of n); and the Brier score's reliability term (Murphy's, over ten
  equal-mass bins of top-label confidence, not estimable under 30 cases a bin). Each is checked
  against numpy, scipy or statsmodels, and `tests/test_stats.py` joins the edit loop
  (`pytest -m core`) without its slow tests.
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
- **Hidden evaluation sets in the dashboard: the Labelling screen** (LP-11m, `#labelling`, over
  a labeller's own routes only). A person holding `label` where those routes are served finds
  **Labelling** in the section list; someone who only labels sees it alone, with no pack, no
  notice and no other section, and without the keys keeps the access page, which says labelling
  is not switched on. It shows their counts set by set, then one case at a time: its input as
  text and the set's questions as radio groups with nothing chosen, then the next case on Save.
  An unanswered question, or a label the server refuses, is said beside the question it names;
  an assignment no longer theirs moves on to the next; when nothing waits, it says so. Blind:
  nothing of the case but its input, nor anyone's label, reaches the page, and nothing is kept
  in the browser's storage. Runbook: `docs/operations/evalsets.md`, How a labeller labels.
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
- **The grading method 1.0, a draft not in effect** (LP-17a, `docs/grading/method-1.0.md`,
  `mendari.grading/1.0`; ADR-0013, Proposed). The rules the scoreboard and its scoped grades will
  follow, written before their code: arms by provider class; selection by declared
  configurations; which figures are estimates with an interval and which are descriptions (ECE
  has no interval and never decides anything); Clopper-Pearson simultaneous across a pack's
  critical labels (`1 - 0.05/L`) for a grade's bound and 95% for display, exact McNemar, the
  paired bootstrap and a stated Holm family; a grade as attestations, each met, not met or
  insufficient evidence; bands for ranking, on frozen hidden sets only; the evidence rule on
  distinct positives (at least 100 for each critical label, 107 for the Clinical Trial
  Prescreener's six at a 0.95 floor); the scope statement, signing, freshness and the developer
  flow. Every number the method sets is in one parameter block, each the owner's to confirm (a
  test checks that the text uses no other), and the effective date is the owner's to set.
  `mendari.evals.method` pins the version, the document's SHA-256 and the parameters; nothing
  uses it yet, so no figure, format or decision changes, and the leaderboards still rank public
  sets with `mendari.rank/1` until LP-17g. Gate `grading-method` now probes the document.
- **Arms by provider class** (`mendari.arms/1`, LP-17c, for the scoreboard):
  `mendari scoreboard arms PACK [--json]` lists a pack's provider configurations, one variant
  per slot, in four arms by the models they ask through any slot or composite member: "Mendari
  only", "+ specialised classifier" (Jev), "+ general LLM" and "+ both". Each configuration
  says whether it runs on the machine, or names the types it calls off it (CQL asks no model
  but is remote). An arm no configuration reaches is "not applicable". It reads the manifest
  alone and writes nothing. Of the reference packs, Patient Inbox Router has 25 configurations,
  Referral Router 20, Clinical Trial Prescreener 10, and Prior Authorization Preflight and AI
  Guard 5 each. Only the two routers reach "+ both", across two slots. Spec:
  `docs/spec/evaluation-reports.md`, Arms.
- **Scoreboard entries and run records** (LP-17d, `mendari.scoreboard/1` and `mendari.run/1`,
  pure: nothing writes or shows them yet). An entry is one configuration's figures on one set,
  computed only from its ledger, its dataset, its run record, a dated price table
  (`mendari.prices/1`) and the grading method, so the same inputs give the same bytes
  (`body_sha256`, without any signature). Each critical label's detection is raw k of n with its
  interval, beside its distinct positives (cases grouped by input under `norm/v1`) and the
  grade's bound (Clopper-Pearson at `1 - 0.05/L` over the distinct groups, a group detected only
  when every positive case in it is). Evidence is decided on distinct positives, each label
  needing the method's minimum (100, or 107 for the Clinical Trial Prescreener's six). An entry
  also holds false automation, the AUTO error rate, automation and accuracy as k of n; macro-F1
  (resampled within each true label), Brier and its reliability term with bootstrap intervals;
  ECE as a description with no interval; cost per 1,000 decisions from the run record's tokens
  and the table (never the ledger's, a pack's or a provider's price), or "0 (no metered calls)",
  or "unknown"; latency p50 and p95 from the run record, "measured on the runner, not
  reproducible"; the declared subgroups ("too few" under 10 cases); and Korn-Graubard weighted
  figures beside the unweighted ones when a set carries sampling weights (detection is never
  weighted). Its scope names the pack, the set, the models as reported, the method and whether it
  is in effect, the date, "engineering evaluation, not clinical validation", the disclosure
  ("not published: disclosure pending" withholds a remote entry's figures) and the pack's status
  line. On a public set it reads "public set: regression check, not a ranking", with no rank and
  no grade. A run record holds each case's latency and tokens by model type and the models
  reported, never a key, an endpoint or case text, and an entry is refused unless it agrees with
  its ledger: its tokens (more only on a case whose provider call failed) and its models (none the
  answers do not name, none they name left out), so tokens are never priced as another model's.
  A refusal of a run record, a price table or the disclosures never echoes a key the input chose.
  A price table is refused from Mendari's code or a pack's directory, and cited only by an entry
  priced from it. `board` reads each entry as its canonical bytes hold it, so entries just built
  and read back show alike; it shows them one table per pack and set, with the
  insufficient-evidence banner, each arm measured, "not measured" or "not applicable", and
  freshness worked out when it is built and never stored ("unsigned", "method draft, not in
  effect", "stale: pack changed" and the like, "stale after DATE"); `render.scoreboard_markdown`
  writes it, with no Rank column and no bare percentage. No reference pack's figures, formats or
  decisions change. Spec: `docs/spec/evaluation-reports.md`.
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
- **A registry that shows only what it checked** (LP-18,
  [ADR-0012](docs/decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)). The
  registry renders each version's Decision Card itself, from the files the pack digest covers,
  with the publisher's text as plain text (no heading, table, image, link or bold line of the
  publisher's can pass for the registry's), and stores and shows no uploaded card, benchmark or
  leaderboard; a version published before serves none (`checked.card: not_rendered`). Every
  version answers `checked` (the signing key, the archive's SHA-256, the digest, the engine,
  the card rendered here, the status label, the review, the namespace's publisher type) apart
  from `claimed` (the publisher, its declared risk level, licence and summary), and the Hub
  shows the two groups apart and renders the card with GFM tables, no raw HTML and no image;
  `figures` stays in the registry's answers, always null. `mendari search` prints no figure and
  says the risk is declared. Where the engine serves or names a pulled pack (its pack detail,
  `mendari card`) it shows the registry's card, without the uploaded benchmark and leaderboard.
- **The registry's chain and reviews** (LP-18b). Every registry write appends a hash-chained
  event naming who acted (`admin`, or a token by its id) and holding the hash of the row it
  left; `mendari registry verify --db` (read-only, exit 1 when broken) checks the chain and
  that every row is the one its event hashed, re-hashes archives and recomputes key ids. A
  registry with rows from before records them once. `GET /v1/chain/head` publishes the head to
  keep elsewhere. A registry reviewer (the admin token until LP-19) records the risk level a
  review found and a one-line note (`POST /v1/packs/{namespace}/{name}/{version}/review`),
  shown as `checked.review` and `claimed.risk.confirmed`.
- **Recalls and deletions** (LP-18c). `mendari yank ns/name@version --reason --severity --key`
  recalls a version with a signed `mendari.yank/1` record; an administrator's recall is signed
  with the registry's own key (`MENDARI_REGISTRY_SIGNING_KEY`, `GET /v1/registry-key`). A
  record is stored only if it names exactly that version, is recent and its signature holds,
  once per version. A recalled version leaves latest and search, is still served by exact
  version with its record, and `GET /v1/yanks` lists every recall. An administrator can delete
  a version (`POST …/delete`): its archive answers `410`, its bytes go, a tombstone stays, and
  `mendari pull` refuses it and removes a cached copy. `mendari pull` of a recalled version
  prints the record and whether its signature holds with a pinned key.
- **Breaking, for deployments that serve pulled packs: a pulled pack waits for its
  organization** (LP-18d). A pull writes where the pack came from beside it
  (`mendari.origin/1`). Served, a pack from a registry never automates until its organization
  approves its exact digest (`GET`/`POST /api/registry-approvals`, `DELETE …/{id}`, the
  `approve` permission, chained `registry_pack.approved` and `.revoked`), at every risk level,
  nor while its pull recorded a recall, and its own rule's BLOCK goes to a person until then.
  Nothing in the pack lifts it, a copy of a reference pack included. `mendari decide --db`
  honours the trail's approvals; `mendari test`, `eval` and `leaderboard --run` check the pack's
  own policy. Approve a pulled pack before relying on its automation.
- **The approval on the Decision Card** (#240). For a pack served from a registry, the card's
  Automation approval section shows where it came from, a recall recorded at pull, the
  approval in force and every earlier one; approving takes a reference and a dialog that names
  the digest and what it never lifts, and revoking is one button, both only with `approve`.
- **A running server honours its registries' recalls** (LP-18e, `server/recalls.py`). A
  decision for one of a registry's packs starts a read of its `GET /v1/yanks` in a thread of its
  own once the last is older than `MENDARI_REGISTRY_RECALL_CHECK_S` (300 s unless set; that
  decision uses the list read before), over https, bounded in time and size, and the server
  holds a recalled pack as an unapproved one is held, and every pack of a registry it has not
  read for `MENDARI_REGISTRY_RECALL_STALE_S` (a day unless set). The full `/api/health` shows
  `registry_recalls`.
- **Rate limits and headers on the registry and the Hub** (LP-18f). The registry limits reads
  per address and writes and site reports per token, in its own database, before a body is
  read (`MENDARI_REGISTRY_RATE_LIMITS`); every registry answer and Hub page carries a strict CSP
  (the Hub's with a fresh nonce), refuses framing and sends HSTS but on this machine. CI's
  `hub-site` runs the Hub's tests over a seeded registry, a hostile example among its packs,
  then the OWASP ZAP baseline, failing on a medium or high risk.
- **The regression check** (LP-18g). On every publish the registry evaluates the version on
  its own test set, in an isolated child process with local providers only, and compares it
  with the newest earlier version's check; it is shown as "publisher's own test set:
  regression check, not a ranking" and never refuses a version
  (`MENDARI_REGISTRY_REGRESSION=off` turns it off).
- **A pulled pack's uploaded benchmark is never a reference** (LP-18h). The Monitoring view and
  `/api/monitoring` take no benchmark for a pack from a registry (`benchmark.reason:
  from_registry`, its benchmark figures null) and run only the checks that need none; the
  evaluation route and `mendari eval` attach no comparison with the benchmark it carries and
  say why (`comparison.status: unknown`), as `mendari demo` does for a pulled copy of its pack
  in the pack cache, while a report given with `--baseline` is still compared. Every other pack
  is monitored and compared as before. The runbook for running a registry is
  [`docs/operations/registry.md`](docs/operations/registry.md).
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
