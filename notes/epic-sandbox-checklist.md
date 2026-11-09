# Epic sandbox checklist (LP-23)

_Written 2026-10-17 for the owner, as B38 says (`tasks/todo.md`): what to register in Epic's free
sandbox, which APIs the packs read, what to note back, and where credentials go. It is written from
Epic's published CDS Hooks and SMART documentation and from Mendari's own code at `06d5332f`;
nothing in it has been tried against Epic yet. The plan is `notes/lp23-plan.md`._

Synthetic patients only: Epic's sandbox patients, never a real patient, and never a vendor's
production endpoint. `<mendari>` below is the address of the Mendari engine the sandbox calls.
It is not the hosted production site: Production gets none of these settings until a pilot is
signed.

## 1. What to register

- **CDS Hooks discovery:** `https://<mendari>/cds-services`.
- **Each service** (one per pack with a standard hook):

  | Service URL | Hook | Pack |
  |---|---|---|
  | `https://<mendari>/cds-services/mendari.clinical-trial-prescreener` | `patient-view` | Clinical Trial Prescreener |
  | `https://<mendari>/cds-services/mendari.prior-auth-preflight` | `order-sign` | Prior Authorization Preflight (with Da Vinci CRD coverage information) |
  | `https://<mendari>/cds-services/mendari.referral-router` | `order-sign` | Referral Router |

  The Patient Inbox Router's service uses Mendari's own hook, `mendari-message-received`, which
  no EHR sends. Do not register it; its SMART launch works without it.
- **Epic's JWK Set URL**, the keys Epic signs its CDS Hooks tokens with. Note it exactly. Its
  origin goes on `MENDARI_FHIR_ALLOWED_SERVERS`, and the URL becomes the `jwks_uri` of Epic's
  entry in `MENDARI_CDS_CLIENTS` (23b, ADR-0014). Mendari fetches keys from that URL only.
- **The SMART app** (ADR-0006, built): the launch URL
  `https://<mendari>/smart/launch?pack=<pack id>`, the redirect URI
  `https://<mendari>/smart/callback`, and the scopes `launch openid fhirUser patient/*.rs` (the
  default of `MENDARI_SMART_SCOPES`). Never `offline_access`: Mendari refuses it.
- **Epic's FHIR base** (the launch's `iss`): on `MENDARI_FHIR_ALLOWED_SERVERS`, and in
  `MENDARI_SMART_EHRS` as `<FHIR base>=<organization>:decide`.

## 2. The APIs the packs read

Read and search, FHIR R4. "Today" is what the packs read at `06d5332f`; the LP-23 step that
starts reading the others is named.

| Resource | Read by | What for |
|---|---|---|
| Patient | every pack | age, sex, preferred language |
| Condition | trial, prior auth | diagnoses |
| Observation | trial, referral | labs (HbA1c and eGFR; the referral's labs) |
| MedicationRequest | trial | medications, since 23k (its start and stop by spec 0.3's stated rule, not yet clinically reviewed) |
| Medication | trial | a MedicationRequest's `medicationReference`, read since 23k |
| MedicationStatement | trial | medications. Epic offers no R4 MedicationStatement API (DSTU2 and STU3 only), and refused the search with a 403 on 2026-11-08: declare it (`MENDARI_EHR_DECLARATIONS`, after the checks in `docs/guides/connecting-an-ehr.md#what-your-ehr-does-not-offer`), or the trial pack abstains |
| DocumentReference | trial, prior auth, referral | clinic notes and visit notes |
| Binary | trial, prior auth | a note's content when the DocumentReference links it, read since 23k |
| Coverage | prior auth | the payer |
| Organization | prior auth | the payer by `Coverage.payor` reference, read since 23k |
| ServiceRequest | prior auth, referral | the order being signed; the referral |
| Communication | inbox | the patient's message (the SMART launch) |
| Procedure | prior auth | prior imaging and therapy |
| DiagnosticReport | referral | ECG and imaging reports |

The services' prefetch templates, which Epic either fills or leaves to Mendari to fetch from
`fhirServer`:

- trial: `Patient/{{context.patientId}}`, `Condition?patient=…`,
  `Observation?patient=…&code=4548-4,33914-3,62238-1`, `MedicationStatement?patient=…`,
  `MedicationRequest?patient=…` (since 23k), `DocumentReference?patient=…&category=clinical-note`;
- prior auth: `Patient/{{context.patientId}}`, `Coverage?patient=…&status=active`,
  `Condition?patient=…`, `Procedure?patient=…`,
  `DocumentReference?patient=…&category=clinical-note`;
- referral: `Patient/{{context.patientId}}`,
  `DocumentReference?patient=…&category=clinical-note`, `DiagnosticReport?patient=…`,
  `Observation?patient=…&category=laboratory`.

## 3. What to note back

- [ ] **`iss`:** its exact value in Epic's CDS Hooks token (B38 says the FHIR base).
- [ ] **`jku`:** the JWK Set URL in the token's header, and whether it is always the same.
- [ ] **`aud`:** its form: the service's URL, the discovery URL, or the base URL alone.
- [ ] **`alg` and `kid`:** RS384 or ES384, and whether the `kid` changes.
- [ ] **`sub` and `tenant`:** whether either is sent, and its value.
- [ ] **The token's lifetime:** its `exp` less its `iat`.
- [ ] **Feedback:** whether Epic sends CDS Hooks feedback (a card accepted or overridden), and
  to which URL.
- [ ] **Hooks and prefetch:** which hooks the sandbox sends (`patient-view`, `order-select`,
  `order-sign`), which prefetch tokens it fills, and whether it sends `fhirServer` and
  `fhirAuthorization` for the rest.
- [ ] **The searches above:** which Epic answers, and which it refuses.
- [ ] **Codings:** the code systems on Conditions, Observations and DocumentReferences, and
  their order (a local code first, then ICD-10-CM or LOINC, say).
- [ ] **Notes:** inline `data` or a `Binary` link, and their content types (plain text, HTML,
  RTF).
- [ ] **The payer:** whether `Coverage.payor` is a reference to an Organization, and that
  Organization's identifier system.
- [ ] **The launch URL:** whether Epic keeps a `?pack=` query on the registered launch URL, or
  needs `appContext` instead.
- [ ] **Embedding:** a new window, Epic's embedded window, or an iframe. Mendari refuses iframes
  today.
- [ ] **Session length:** the access token's lifetime, and whether Epic needs refresh tokens.
  Mendari keeps none today.
- [ ] **Scopes:** whether Epic grants wildcard scopes (`patient/*.rs`), and the
  `scopes_supported` its `.well-known/smart-configuration` publishes.
- [ ] **External CDS services:** whether the free sandbox can call a CDS service outside Epic at
  all. If it cannot, the Epic path is proven with test keys and Inferno only until a pilot site's
  own environment exists (owner decision 15), and the scorecard says so.
- [ ] **`mendari smart check`:** its output against Epic's FHIR base, with sign-in on.
- [ ] **The 2026-11-08 decision's counts** (counts only, from its data-coverage record; reading
  the hosted database is a production read, so the owner's): `fields.notes.unreadable` (`cap`
  and `unsupported_type`: equal counts mean each `unsupported_type` was an RTF twin; 20 plus the
  `cap` count is the patient's note count, which says whether 50 is enough),
  `lists.medications.sources.MedicationStatement.unreadable` (expected `unauthorized: 1`), and
  the MedicationRequest source's `unreadable` (`cap`: the orders whose drug was not read). From
  the next run on, the record also gives `prefetch` (the status of each failed prefetch read),
  `links` (reads by budget, failed reads by status) and `unread_formats` (how many notes were
  RTF, PDF or XML only).
- [ ] **Medications on Epic:** whether patient-reported and historical home medications come back
  as MedicationRequest, and with which `intent` (PR #287 leaves out `proposal`, `plan` and
  `option`); whether the default MedicationRequest search returns stopped, completed and
  inpatient orders; whether `Medication.code.text` or the request's `display` carries the generic
  name; whether one drug ordered twice names one Medication id. All four before declaring
  MedicationStatement.
- [ ] **Notes on Epic:** whether each Clinical Notes DocumentReference gives the same note as
  `text/html` and `text/rtf` Binaries (assumed, unconfirmed), and how many are RTF only.
- [ ] **Epic's CDS prefetch for MedicationStatement:** whether it sends nothing, null, an empty
  search or an OperationOutcome for the key it cannot fill.
- [ ] **`_include=MedicationRequest:medication`:** whether Epic's MedicationRequest search
  supports it (a pack change could then read the drugs in the paged search).

What is public (the `iss`, the JWK Set URL, the `aud` form, the hook list, the scopes) can go in
the pull request that uses it. Nothing secret can.

## 4. Where credentials go

Client ids, client secrets, private keys, sandbox logins and any token go in
`~/Documents/mendari-private`, never in the repository, a pull request, an issue or a chat, and not
in the hosted site's settings until a pilot is signed. Mendari's own test keys are made at run time
and never committed.

## 5. Oracle Health, the backup

If Epic's sandbox cannot call an external CDS service, or Epic's terms stop the pilot, the same
checklist applies to Oracle Health's sandbox, with the same notes back. A launch there ends with
its shorter token (ADR-0006).

## 6. After the run

- The Epic sandbox run is the owner's acceptance: every reference pack with a standard hook
  decides on Epic's sandbox patients without a failed hook (vision audit, A12.acc-3). A build pull
  request then removes the gate's pending line (23m).
- The notes back fill in Epic's `MENDARI_CDS_CLIENTS` entry (23b) and the scorecard's vendor fit
  rows (`notes/ehr-scorecard.md`).

## 7. Runs

- **2026-11-08, SMART launch on Epic's public sandbox (the owner).** Epic's app (public client,
  SMART v2, R4) was granted the R4 APIs for Patient, Condition, Observation (labs),
  MedicationRequest, Medication, DocumentReference and Binary (clinical notes), Coverage,
  Organization, ServiceRequest, Procedure and DiagnosticReport; there is no R4 MedicationStatement
  API and no Communication API for patient messages. The launch, Epic's authorization, the
  callback (id_token verified, an `ehr` principal) and the patient read worked. Deciding the
  trial pack for Epic's test patient abstained, with five reasons and three causes:
  - `medications: unauthorized`: the MedicationStatement search, which Epic refused (403);
  - `medications.code: cap`, `medications.name: cap`, `notes: cap`: one budget of 20 links a
    decision, which the notes, read first, used up;
  - `notes: unsupported_type`: the RTF twin of each capped HTML note, counted again (assumed:
    each Epic note an HTML and an RTF Binary).

  Built in the Epic data fit pull request (branch `claude/epic-data-fit`): budgets by purpose (50
  notes, 50 followed references) with a request bound and clocks, a note's renditions counted
  once, the coverage record's `prefetch`, `links` and `unread_formats`, and
  `MENDARI_EHR_DECLARATIONS` (off by default). With the declaration set, a chart is decided and
  goes to a person (risk level 2) when it has: up to 50 notes whose text fits in 20,000
  characters, each with an HTML or plain-text rendition (a note in RTF only abstains,
  `unsupported_type`); at most 50 distinct drugs among its listed MedicationRequests, each
  Medication read; every prefetch search (Condition, Observation and DocumentReference as well as
  MedicationRequest) of at most 11 pages (`cap`); the linked reads done within 60 s and the
  decision's deadline (`timeout`); and, once #287 lands, no medication recorded with `intent`
  `plan`. On a CDS hook, a MedicationStatement prefetch the EHR sends as an OperationOutcome still
  abstains (`operation_outcome`). Without the declaration, the trial pack abstains on
  MedicationStatement alone, by design. The run stays the
  owner's acceptance; setting the declaration on the hosted site is the owner's too.
