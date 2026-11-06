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
| MedicationStatement | trial | medications |
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
