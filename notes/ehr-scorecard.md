# EHR scorecard (rubric v1)

_Graded 2026-10-17 at `06d5332f` with LP-23a, from the vision audit's A12 rows
(`notes/vision-audit.md`, "A12. EHR pilot readiness") and the code, for LP-23
(`notes/lp23-plan.md`). "With LP-23a" means main's code at `06d5332f`, plus what LP-23a adds to
it: the synthetic trial and prior-authorization records, the golden of today's FHIR decisions,
and tests that show behaviour `06d5332f` already had. A row that rests on any of it names 23a in
its last column; every other row's tests are all at `06d5332f`. Each LP-23 step that meets a
criterion updates its row with the test that shows it and names itself in the row's last
column (`23d: ...`); a grade's level is always the rule applied to the rows as they stand, and the
step that closes LP-23 (23m) grades every dimension again at its own commit._

**The scope of every grade:** rubric v1, graded at `06d5332f` with LP-23a, synthetic data and
test keys only, not a vendor certification. A rubric level is an assessment against named
criteria, not an estimate from a sample, so it has no interval.

**The grade the launch plan cites.** The launch plan puts Mendari's EHR readiness at 2.5 on a
five-point scale. That grade and its rubric are not in the repository or anywhere in its history
(vision audit, A12.goal), so it is not reproduced here and nothing below is compared with it.
The owner approves this rubric, or supplies the original (owner decision 3 in the plan).

## The rubric (v1)

Seven dimensions, each graded on five levels by named criteria:

| Level | What it means |
|---|---|
| 1 | Below the floor: a level 2 criterion is not met. |
| 2 | Fails safe: with sign-in on nothing wrong is accepted or shown, but the EHR's own way of working does not work yet. |
| 3 | Works the EHR's own way, end to end, on synthetic data and test keys, in Mendari's own tests. |
| 4 | Shown by an outside client or validator in CI: Inferno, the HL7 validator, a CDS client over HTTP. |
| 5 | Shown on the named vendor's sandbox (Epic, with Oracle Health as the backup). This waits on the owner. |

- A dimension is at the highest level whose criteria, and every lower level's, are all met.
  Level 1 needs none. Every dimension has criteria at each level from 2 to 5.
- A criterion is met, partial or missing. Partial counts as not met.
- **Evidence** is the test that shows the criterion, or "missing".
- **Source** is the vision audit's row, or the LP-23 requirement, the criterion comes from.
- **Built in, or waits on** names the LP-23 step that builds an unmet criterion, or says what the
  owner has to do first. On a met criterion it names the LP-23 step whose work the row rests on,
  and is empty when the row rests on `06d5332f` alone.
- **The overall grade is the lowest dimension's level**, shown beside every dimension, so a
  strong dimension cannot hide a missing one.

## Summary

| Dimension | Level | Overall | Scope |
|---|---|---|---|
| 1. Client trust | 2 | 1 | rubric v1, graded at `06d5332f` with LP-23a, synthetic data and test keys only, not a vendor certification |
| 2. Who may call from where | 3 | 1 | rubric v1, graded at `06d5332f` with LP-23a, synthetic data and test keys only, not a vendor certification |
| 3. Failure behaviour | 1 | 1 | rubric v1, graded at `06d5332f` with LP-23a, synthetic data and test keys only, not a vendor certification |
| 4. Data shapes | 2 | 1 | rubric v1, graded at `06d5332f` with LP-23a, synthetic data and test keys only, not a vendor certification |
| 5. Cards and workflow | 2 | 1 | rubric v1, graded at `06d5332f` with LP-23a, synthetic data and test keys only, not a vendor certification |
| 6. Conformance evidence | 2 | 1 | rubric v1, graded at `06d5332f` with LP-23a, synthetic data and test keys only, not a vendor certification |
| 7. Vendor fit | 2 | 1 | rubric v1, graded at `06d5332f` with LP-23a, synthetic data and test keys only, not a vendor certification |

**Overall:** level 1, the lowest dimension (failure behaviour; who may call from where was
level 1 too until LP-23c, and cards and workflow until LP-23f). Rubric v1, graded at `06d5332f`
with LP-23a, synthetic data and test keys only, not a vendor certification.

## 1. Client trust

Who the caller is: an EHR's own signed token, verified against the keys registered for it.

| Id | Level | Criterion | Status | Evidence | Source | Built in, or waits on |
|---|---|---|---|---|---|---|
| CT-2a | 2 | With sign-in on, discovery and a hook need a credential Mendari issued, and a key scoped to deciding reads discovery and calls a hook. | met | `tests/test_ehr_baseline.py::test_with_sign_in_on_discovery_and_a_hook_need_a_credential_mendari_issued` | A12.1c | 23a: the test (the behaviour is `06d5332f`'s) |
| CT-2b | 2 | With sign-in on, a bearer credential Mendari did not issue (an EHR's JWT among them) is refused with `401` and never read as an identity. | met | `tests/test_ehr_baseline.py::test_an_ehrs_signed_token_is_refused_as_a_credential_mendari_did_not_issue`, `tests/test_auth.py::test_a_credential_that_is_not_valid_never_falls_back_to_the_session` | A12.ctx-jwt-today | 23a: the EHR token's test (the behaviour is `06d5332f`'s). From 23b this holds for an EHR that is not registered (`MENDARI_CDS_CLIENTS`), and a registered EHR's token is verified instead (CT-3a) |
| CT-3a | 3 | An EHR's signed token (RS384 or ES384) is verified against the keys registered for its issuer: the signature, `iss`, `aud`, `exp`, `iat`, a `jti` replay check, and `jku` only when it equals the registered URI. | missing | missing | A12.1a | 23b: built there (`tests/test_cds_clients.py`, `tests/test_cds_auth.py`), and met when 23m grades this row again at its own commit |
| CT-3b | 3 | A verified token is a narrow `cds` principal in the registered organization, holding only discover and decide, and reaching `/cds-services` only. | missing | missing | A12.1b | 23b: built there (`tests/test_cds_auth.py`, and the route matrix in `tests/test_auth.py`), and met when 23m grades this row again at its own commit |
| CT-3c | 3 | The same registered client reads discovery and calls a hook. | partial | `tests/test_key_presets.py::test_the_documented_calls_answer_with_the_presets_keys` | A12.1c | 23b: true at `06d5332f` for a Mendari key, not for an EHR's token; 23b makes it true for a registered EHR's own tokens too, a new token for each request since a `jti` is used once (`tests/test_cds_auth.py`), and 23m grades it again |
| CT-4a | 4 | An outside CDS client (Inferno's Da Vinci CRD kit) calls over HTTP with signed tokens in CI, and Mendari verifies them. | missing | missing | A12.acc-2 | 23l |
| CT-5a | 5 | Epic's own sandbox token is verified and accepted. | missing | missing | A12.goal | the owner: Epic's sandbox registration, and its token's `iss`, `jku` and `aud` noted back (`notes/epic-sandbox-checklist.md`) |

**Grade:** level 2. **Overall:** level 1. Rubric v1, graded at `06d5332f` with LP-23a,
synthetic data and test keys only, not a vendor certification.

## 2. Who may call from where

Which hosts and browser origins may reach the CDS services, with sign-in on and off.

| Id | Level | Criterion | Status | Evidence | Source | Built in, or waits on |
|---|---|---|---|---|---|---|
| WC-2a | 2 | A request from another site reaches only the public routes and pages. | met | `tests/test_origin.py::test_another_sites_requests_reach_exactly_the_public_routes_and_pages` | A12.1d | |
| WC-2b | 2 | With sign-in off (open mode), `/cds-services` answers only on this machine or under an explicit sandbox flag. | met | `tests/test_cds_open_mode.py::test_open_mode_answers_cds_hooks_on_this_machine_only`, `tests/test_cds_open_mode.py::test_a_spoofed_host_or_forwarded_header_is_not_this_machine`, `tests/test_cds_open_mode.py::test_the_sandbox_flag_answers_from_anywhere_and_stops_startup_on_vercel_or_with_sign_in`, `tests/test_cds_open_mode.py::test_a_verified_token_does_not_lift_the_rule_in_open_mode` | A12.1e | 23c: a loopback address, a local `Host` and no forwarding header, else `403` (`open_mode_remote`); `MENDARI_CDS_SANDBOX=1` refused on Vercel and with sign-in on (open mode answered any host before, a forged token included until 23b) |
| WC-3a | 3 | CORS on `/cds-services` and its children only, for registered EHR origins, without credentials. | met | `tests/test_cds_cors.py::test_a_registered_origin_gets_cors_on_cds_services_only`, `tests/test_cds_cors.py::test_a_preflight_is_answered_before_the_guard_without_credentials`, `tests/test_cds_cors.py::test_an_unregistered_origin_gets_no_cors_and_is_refused_as_before`, `tests/browser/test_cds_cors.py::test_a_page_on_a_registered_origin_reads_a_hook_answer` | A12.1d | 23c: `server/cors.py`, the clients' `origins` |
| WC-3b | 3 | A cross-site `Origin` on a CDS client's token must be one of that client's own origins. | met | `tests/test_cds_cors.py::test_an_origin_registered_for_another_client_is_refused`, `tests/test_cds_cors.py::test_an_unregistered_origin_gets_no_cors_and_is_refused_as_before` | A12.1d | 23c: `CdsRegistry.from_its_origin`, a `403` (`cds_origin`) before the `jti` is claimed |
| WC-4a | 4 | Over HTTP from another container in CI, a signed CDS client is answered by the signed-in engine, and only the sandbox's flagged engine answers in open mode. | missing | missing | A12.5a | 23e: 23c set the flag on the compose sandbox's open engine alone; the call from another container in CI is 23e's |
| WC-5a | 5 | Epic's sandbox reaches Mendari's CDS services from Epic's own servers. | missing | missing | A12.acc-3 | the owner: whether Epic's free sandbox can call an external CDS service at all (owner decision 15) |

**Grade:** level 3. **Overall:** level 1. Rubric v1, graded at `06d5332f` with LP-23a,
synthetic data and test keys only, not a vendor certification.

## 3. Failure behaviour

What a hook does when the request is wrong, or the record cannot be read.

| Id | Level | Criterion | Status | Evidence | Source | Built in, or waits on |
|---|---|---|---|---|---|---|
| FB-2a | 2 | A request fault (a malformed CDS request, a `fhirServer` off the allow-list) is a `4xx`, and nothing is fetched. | met | `tests/test_fhir_client.py::test_a_malformed_cds_request_is_a_client_error`, `tests/test_fhir_client.py::test_a_server_off_the_list_is_refused` | A12.ctx-fhirAuthorization | |
| FB-2b | 2 | Input that is not FHIR R4 JSON (an unknown element or resource type, a wrong JSON type, a malformed primitive) is refused before any provider runs, in every mode. | met | `tests/test_fhir_structure.py::test_a_malformed_primitive_is_a_structure_error_in_every_mode` | A12.3f | |
| FB-2c | 2 | A failed read fails closed, and never echoes the server's answer. | met | `tests/test_fhir_client.py::test_failures_fail_closed_without_echoing_the_server` | A12.3f | |
| FB-2d | 2 | Input that is R4 JSON but breaks R4's other rules (a complex element R4 requires, a code outside an R4 required value set) is refused before any provider runs. | partial | `tests/test_fhir_structure.py::test_base_r4_findings_follow_fhir_validation`, `tests/test_us_core.py::test_the_setting`, `tests/test_us_core.py::test_the_server_reports_the_mode_and_returns_the_issues` | A12.3f | the owner: the pilot's `MENDARI_FHIR_VALIDATION`. Only `enforce` blocks such input; under `warn`, the default, it is decided as if it conformed, with its issues recorded on the decision and audited, and `off` skips the check |
| FB-3a | 3 | A failed prefetch read gives a recorded ABSTAIN with its reason, and the hook still answers. | met | `tests/test_cds_unreadable.py::test_a_fetch_failure_gives_abstain_with_a_reason_and_a_recorded_decision`, `tests/test_cds_unreadable.py::test_a_search_page_that_is_not_r4_abstains_never_a_500`, `tests/test_cds_unreadable.py::test_smart_decide_abstains_on_a_failed_prefetch_read` | A12.3f | 23d: each key is read on its own, and one that fails abstains with a fixed reason (it was a `422`, with nothing recorded) |
| FB-3b | 3 | Data present but unreadable (a note that is only a link, or whose data does not decode) gives ABSTAIN with its reason. | met | `tests/test_cds_unreadable.py::test_a_note_that_is_only_a_link_or_does_not_decode_abstains` | A12.3a | 23d: such a note abstains (`not_fetched`, `undecodable`); before, it read as no notes |
| FB-3c | 3 | Every FHIR decision carries its data-coverage record: for each mapped field, what was looked for and what was found, as counts and declared codes. | met | `tests/test_data_coverage.py::test_every_fhir_decision_carries_its_data_coverage`, `tests/test_data_coverage.py::test_no_note_text_patient_code_or_id_reaches_the_record_or_the_chain` | LP-23 requirement 7 | 23d: `data_coverage`, `mendari.data-coverage/1` |
| FB-4a | 4 | Inferno's CRD kit passes its technical-issues group: a hook whose reads fail answers, with `indeterminate` coverage for a technical reason. | missing | missing | A12.acc-1 | 23l: 23d built the `indeterminate` coverage (`scripts/davinci_sample.py`), and Inferno's run is 23l's |
| FB-5a | 5 | On Epic's sandbox, a read that fails or a note Mendari cannot read abstains with its reason, and the data-coverage counts show how often. | missing | missing | A12.acc-3 | the owner: the Epic sandbox run |

**Grade:** level 1. **Overall:** level 1. Rubric v1, graded at `06d5332f` with LP-23a,
synthetic data and test keys only, not a vendor certification.

## 4. Data shapes

Whether the mappings read the shapes an EHR sends.

| Id | Level | Criterion | Status | Evidence | Source | Built in, or waits on |
|---|---|---|---|---|---|---|
| DS-2a | 2 | Each mapped pack decides its synthetic FHIR cases as pinned (a golden of today's decisions and input hashes). | met | `tests/test_fhir_decisions.py::test_every_mapped_pack_decides_its_synthetic_fhir_as_the_golden_says` | A12.ctx-prefetch | 23a: the golden and its test |
| DS-2b | 2 | A synthetic FHIR record decides as the same case's JSON input does (referral, trial and prior authorization), but for a prior-authorization order the pack has no requirement set for (FX901 to FX903): through FHIR it gets no decision (no order passes the mapping's `focus_where`, so the input is refused as missing its ServiceRequest), where its JSON record decides HUMAN_REVIEW, as service `other`. | met | `tests/test_sandbox.py::test_referral_cases_decide_the_same_from_fhir`, `tests/test_fhir_decisions.py::test_the_new_synthetic_fhir_decides_as_its_record_does` | A12.acc-3 | 23a: the trial and prior-authorization records and their test |
| DS-2c | 2 | Prefetch search Bundles are flattened and deduplicated, and a key the EHR left out is fetched from an allow-listed `fhirServer` with the EHR's token. | met | `tests/test_fhir_lists.py::test_prefetch_search_bundles_are_flattened_and_deduplicated`, `tests/test_fhir_client.py::test_cds_hooks_fetches_missing_prefetch_from_an_allowed_server` | A12.ctx-prefetch | |
| DS-3a | 3 | A coding is chosen by its code system, wherever it sits. | missing | missing | A12.3b | 23g, 23k: today the first coding only (`tests/test_trial_pack.py::test_through_fhir_a_condition_is_read_by_its_first_coding_only`) |
| DS-3b | 3 | A note linked by `Binary` is read through the allow-listed client. | missing | missing | A12.3a | 23h, 23k |
| DS-3c | 3 | The payer is read by its Coverage reference. | missing | missing | A12.3c | 23h, 23k |
| DS-3d | 3 | MedicationRequest is read, with a stated rule for its start and stop. | missing | missing | A12.3d | 23g, 23k, and the owner: the go-ahead for 23k, with a clinician's note on the rule (owner decision 5) |
| DS-3e | 3 | A site's own codes are translated by a per-organization site code map, versioned and hashed into each decision. | missing | missing | A12.3e | 23i |
| DS-4a | 4 | A vendor-shaped synthetic profile (linked notes, several codings, MedicationRequest) passes the HL7 validator against US Core 6.1.0 in CI, and every pack with a standard hook decides on it without a failed hook. | missing | missing | LP-23 requirement 10 | 23e |
| DS-5a | 5 | Every reference pack with a standard hook decides on Epic's sandbox patients without a failed hook. | missing | missing | A12.acc-3 | the owner: the Epic sandbox run |

**Grade:** level 2. **Overall:** level 1. Rubric v1, graded at `06d5332f` with LP-23a,
synthetic data and test keys only, not a vendor certification.

## 5. Cards and workflow

What the clinician sees, and what comes back from them.

| Id | Level | Criterion | Status | Evidence | Source | Built in, or waits on |
|---|---|---|---|---|---|---|
| CW-2a | 2 | Discovery and every card say the pack's status and the notice. | met | `tests/test_pack_status.py::test_cds_hooks_say_it_in_discovery_and_on_every_card`, `tests/test_notice.py::test_cds_hooks_carry_it` | A12.2c | |
| CW-2b | 2 | The trial, prior-authorization and referral cards use their pack's own plain-language summaries. | met | `tests/test_trial_pack.py::test_patient_view_card`, `tests/test_prior_auth_pack.py::test_order_sign_gives_a_card_per_covered_order`, `tests/test_referral_pack.py::test_order_sign_gives_one_card_per_referral` | A12.2c | |
| CW-2c | 2 | A pack can decide and audit without showing a card (`show`), as the trial pack does for a patient who is clearly not eligible. | met | `tests/test_fhir_lists.py::test_patient_view_cards_use_the_packs_wording`, `tests/test_trial_pack.py::test_patient_view_card` | A12.2e | |
| CW-2d | 2 | No card shows a bare percentage. | met | `tests/test_cds_cards.py::test_no_card_shows_a_bare_percentage` | Definition of shipped §3 | 23f: the confidence, shown as a bare percentage before, left the card's detail in every configuration |
| CW-3a | 3 | A card with suggestions says its `selectionBehavior`. | met | `tests/test_cds_cards.py::test_a_card_with_suggestions_says_its_selection_behavior` | A12.2a | 23f: `at-most-one`, with the override reasons of Mendari's own CodeSystem |
| CW-3b | 3 | A feedback endpoint records accepted and overridden cards in the chain. | met | `tests/test_cds_feedback.py::test_feedback_for_an_accepted_and_an_overridden_card_is_recorded_and_chained`, `tests/test_cds_feedback.py::test_feedback_naming_another_organizations_packs_or_callers_decision_answers_as_unknown` | A12.2b | 23f: `POST /cds-services/{id}/feedback`, chained as `card.feedback`, codes and ids only |
| CW-3c | 3 | A card's words use the pack's display names, and the engine's detail sits behind a link to the audit view. | met | `tests/test_cds_cards.py::test_card_summaries_use_display_names_and_task_text_is_unchanged`, `tests/test_cds_cards.py::test_the_engine_trace_is_behind_the_audit_link_only_when_one_can_be_made` | A12.2c-audit-link | 23f: the link when `MENDARI_PUBLIC_URL` is set; without it, the reasons stay in the detail, but for the confidence thresholds' |
| CW-3d | 3 | A card links to the EHR launch view. | met | `tests/test_cds_cards.py::test_a_card_links_to_the_launch_view_only_for_a_registered_ehr_of_the_same_organization` | A12.2d | 23f: a `smart` link with `?pack=`, when SMART is on and the `fhirServer` is a launch-registered EHR of the caller's organization |
| CW-3e | 3 | Referral cards show only when action is needed, and the inbox pack has a card template. | partial | `tests/test_trial_pack.py::test_patient_view_card` | A12.2e, A12.2c | 23j: the mechanism exists, and the referral and inbox packs do not use it yet; the owner confirms the referral `show` list (owner decision 4) |
| CW-4a | 4 | Every card matches the CDS Hooks 2.0 card shape, and Inferno's CRD card tests pass. | missing | missing | A12.acc-1 | 23l: 23f checks every card against CDS Hooks 2.0's card shape in Mendari's own tests, and a CRD service's cards carry `source.topic`; Inferno's CRD card tests are 23l's |
| CW-5a | 5 | Epic's sandbox shows the cards and sends feedback on them. | missing | missing | A12.2b | the owner: the Epic sandbox run, and whether Epic sends feedback (`notes/epic-sandbox-checklist.md`) |

**Grade:** level 2. **Overall:** level 1. Rubric v1, graded at `06d5332f` with LP-23a,
synthetic data and test keys only, not a vendor certification.

## 6. Conformance evidence

What outside tools have checked, in CI.

| Id | Level | Criterion | Status | Evidence | Source | Built in, or waits on |
|---|---|---|---|---|---|---|
| CE-2a | 2 | The HL7 validator runs offline in CI over Mendari's outputs and the sandbox's inputs (the `fhir-conformance` job), over the same files the in-process US Core checks pass. | met | `tests/test_ehr_baseline.py::test_ci_runs_the_hl7_validator_offline_over_mendaris_outputs_and_the_sandboxs_inputs` | A12.5d | 23a: the test (the job is `06d5332f`'s) |
| CE-2b | 2 | The SMART app runs against a third-party launcher (smart-launcher-v2) in CI (the `sandbox-e2e` job). | met | `tests/test_ehr_baseline.py::test_ci_runs_the_smart_app_against_smart_launcher_v2` | A12.5b | 23a: the test (the job is `06d5332f`'s) |
| CE-3a | 3 | A CDS Hooks client calls the services over HTTP with signed tokens, in the sandbox and in CI. | missing | missing | A12.5a | 23e |
| CE-4a | 4 | Inferno's Da Vinci CRD kit runs as the CDS client in CI, with each test's outcome pinned with its reason. | missing | missing | A12.acc-1, A12.acc-2 | 23l |
| CE-4b | 4 | Inferno's SMART App Launch and Bulk Data client suites run for Mendari's roles. | missing | missing | A12.5b, A12.5c | 23l |
| CE-4c | 4 | The HL7 validator also runs with a terminology server, on a schedule. | missing | missing | A12.5d | 23l: today it runs offline only, on every change (CE-2a) |
| CE-4d | 4 | The scopes Mendari asks for are checked against the scopes the EHR publishes (`scopes_supported`). | missing | missing | A12.5e | 23l |
| CE-5a | 5 | `mendari smart check` and the scope check pass against Epic. | missing | missing | A12.5e | the owner: `mendari smart check` run against Epic's FHIR base |

**Grade:** level 2. **Overall:** level 1. Rubric v1, graded at `06d5332f` with LP-23a,
synthetic data and test keys only, not a vendor certification.

## 7. Vendor fit

Session length, embedding and the launch's form, as the named vendor needs them.

| Id | Level | Criterion | Status | Evidence | Source | Built in, or waits on |
|---|---|---|---|---|---|---|
| VF-2a | 2 | A launch ends with the EHR's token: `offline_access` is refused, so no refresh token is kept. | met | `tests/test_smart_sealing.py::test_offline_access_is_refused_at_launch` | A12.4 | |
| VF-2b | 2 | Inside an iframe the app fails safely (`frame-ancestors 'none'` on every response). | met | `tests/test_security_headers.py::test_every_response_carries_the_headers` | A12.4 | |
| VF-2c | 2 | A SMART launch with `?pack=` opens the patient's view, in a new window or the EHR's embedded window. | met | `tests/test_smart.py::test_an_ehr_launch_opens_the_patients_view_with_a_server_side_session` | A12.2d | |
| VF-3a | 3 | A session lasts as long as the vendor needs (refresh tokens, if Epic needs them). | missing | missing | A12.4 | the owner: the Epic sandbox's token lifetime, and whether Epic needs refresh tokens (owner decision 13) |
| VF-3b | 3 | The app embeds as the vendor needs (in an iframe, if Epic needs one). | missing | missing | A12.4 | the owner: how Epic's sandbox opens the app (owner decision 13) |
| VF-3c | 3 | The launch URL is in the form the vendor registers (`?pack=` or `appContext`). | partial | `tests/test_smart.py::test_an_ehr_launch_opens_the_patients_view_with_a_server_side_session` | A12.2d | the owner: whether Epic keeps the `?pack=` query (owner decision 12) |
| VF-4a | 4 | Inferno's SMART App Launch client suite passes for the vendor's launch form. | missing | missing | A12.5b | 23l |
| VF-5a | 5 | A launch and a hook from Epic's sandbox work end to end. | missing | missing | A12.acc-3 | the owner: the Epic sandbox run |

**Grade:** level 2. **Overall:** level 1. Rubric v1, graded at `06d5332f` with LP-23a,
synthetic data and test keys only, not a vendor certification.
