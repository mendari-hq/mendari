# EHR scorecard (rubric v1)

_Graded twice, before LP-23 and at its close-out, from the vision audit's A12 rows
(`notes/vision-audit.md`, "A12. EHR pilot readiness") and the code, for LP-23
(`notes/lp23-plan.md`)._

- _**Before:** graded 2026-10-17 at `06d5332f` with LP-23a. "With LP-23a" means main's code at
  `06d5332f`, plus what LP-23a adds to it: the synthetic trial and prior-authorization records,
  the golden of today's FHIR decisions, and tests that show behaviour `06d5332f` already had. The
  Before column is each criterion's status as LP-23a merged it (#256)._
- _**After:** graded 2026-11-06 at the close-out commit, LP-23m's, whose parent is the LP-23
  lane's `d2ef0201`. A commit cannot hold its own hash, so the scope names it `d2ef0201` with
  LP-23m, as the before column names `06d5332f` with LP-23a. Each row is graded only on evidence
  in the repository: a test, a golden, or a run recorded there. A row whose evidence would be a
  CI run that has not happened yet is not met, and its last column says which run: `sandbox-e2e`
  and `fhir-conformance` run on the LP-23 lane's pull request, and `conformance.yml` and
  `terminology.yml` run on main on a schedule or by hand only, so their first runs come after the
  lane merges._
- _The Evidence column and the last column are the after grade's. A test a row cites that was not
  at `06d5332f` is a test of LP-23a's or of a later step's, which the row's last column names
  first (`23d: ...`). A grade's level is always the rule applied to its own column._

**The scope of every grade:** rubric v1; before graded at `06d5332f` with LP-23a, after at
`d2ef0201` with LP-23m (the close-out commit); synthetic data and test keys only, not a vendor
certification. A rubric level is an assessment against named criteria, not an estimate from a
sample, so it has no interval.

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
- **Before** and **After** are the criterion's status at each grading.
- **Evidence** is the test that shows the criterion at the close-out, or "missing".
- **Source** is the vision audit's row, or the LP-23 requirement, the criterion comes from.
- **Built in, or waits on** names the LP-23 step that builds an unmet criterion, or says what the
  owner has to do first. On a met criterion it names the LP-23 step whose work the row rests on,
  and is empty when the row rests on `06d5332f` alone. On a partial one that cites a later
  step's tests, it names that step first, then what the rest waits on.
- **The overall grade is the lowest dimension's level**, shown beside every dimension, so a
  strong dimension cannot hide a missing one.

## Summary

| Dimension | Before | After | Overall, before | Overall, after | Scope |
|---|---|---|---|---|---|
| 1. Client trust | 2 | 3 | 1 | 1 | rubric v1; before graded at `06d5332f` with LP-23a, after at `d2ef0201` with LP-23m (the close-out commit); synthetic data and test keys only, not a vendor certification |
| 2. Who may call from where | 1 | 3 | 1 | 1 | rubric v1; before graded at `06d5332f` with LP-23a, after at `d2ef0201` with LP-23m (the close-out commit); synthetic data and test keys only, not a vendor certification |
| 3. Failure behaviour | 1 | 1 | 1 | 1 | rubric v1; before graded at `06d5332f` with LP-23a, after at `d2ef0201` with LP-23m (the close-out commit); synthetic data and test keys only, not a vendor certification |
| 4. Data shapes | 2 | 2 | 1 | 1 | rubric v1; before graded at `06d5332f` with LP-23a, after at `d2ef0201` with LP-23m (the close-out commit); synthetic data and test keys only, not a vendor certification |
| 5. Cards and workflow | 1 | 2 | 1 | 1 | rubric v1; before graded at `06d5332f` with LP-23a, after at `d2ef0201` with LP-23m (the close-out commit); synthetic data and test keys only, not a vendor certification |
| 6. Conformance evidence | 2 | 2 | 1 | 1 | rubric v1; before graded at `06d5332f` with LP-23a, after at `d2ef0201` with LP-23m (the close-out commit); synthetic data and test keys only, not a vendor certification |
| 7. Vendor fit | 2 | 2 | 1 | 1 | rubric v1; before graded at `06d5332f` with LP-23a, after at `d2ef0201` with LP-23m (the close-out commit); synthetic data and test keys only, not a vendor certification |

**Overall:** level 1 before, level 1 after: the lowest dimension both times is failure
behaviour, held at level 1 by FB-2d, the pilot's `MENDARI_FHIR_VALIDATION` (owner decision 16).
Before, who may call from where and cards and workflow were at level 1 too. Rubric v1; before
graded at `06d5332f` with LP-23a, after at `d2ef0201` with LP-23m (the close-out commit);
synthetic data and test keys only, not a vendor certification.

**Graded again, not carried over.** The after column is not the rows as each step left them.
23e marked WC-4a and CE-3a met on the CI job that runs its signed client (`sandbox-e2e`, pinned by
`tests/test_cds_client_sandbox.py::test_ci_runs_the_signed_client_against_both_engines`), but that
job has not run with the client on GitHub yet. After, both are partial until the lane's pull
request runs it, so who may call from where is at level 3, not 4, and conformance evidence at
level 2, not 3. Rows met by a CI job that has run on main since before LP-23 (CE-2a, CE-2b) stay
met.

## 1. Client trust

Who the caller is: an EHR's own signed token, verified against the keys registered for it.

| Id | Level | Criterion | Before | After | Evidence | Source | Built in, or waits on |
|---|---|---|---|---|---|---|---|
| CT-2a | 2 | With sign-in on, discovery and a hook need a credential Mendari issued, and a key scoped to deciding reads discovery and calls a hook. | met | met | `tests/test_ehr_baseline.py::test_with_sign_in_on_discovery_and_a_hook_need_a_credential_mendari_issued` | A12.1c | 23a: the test (the behaviour is `06d5332f`'s) |
| CT-2b | 2 | With sign-in on, a bearer credential Mendari did not issue (an EHR's JWT among them) is refused with `401` and never read as an identity. | met | met | `tests/test_ehr_baseline.py::test_an_ehrs_signed_token_is_refused_as_a_credential_mendari_did_not_issue`, `tests/test_auth.py::test_a_credential_that_is_not_valid_never_falls_back_to_the_session` | A12.ctx-jwt-today | 23a: the EHR token's test (the behaviour is `06d5332f`'s). From 23b this holds for an EHR that is not registered (`MENDARI_CDS_CLIENTS`), and a registered EHR's token is verified instead (CT-3a) |
| CT-3a | 3 | An EHR's signed token (RS384 or ES384) is verified against the keys registered for its issuer: the signature, `iss`, `aud`, `exp`, `iat`, a `jti` replay check, and `jku` only when it equals the registered URI. | missing | met | `tests/test_cds_clients.py::test_a_valid_token_is_accepted`, `tests/test_cds_clients.py::test_a_wrong_issuer_is_refused`, `tests/test_cds_clients.py::test_a_wrong_audience_is_refused`, `tests/test_cds_clients.py::test_an_expired_token_is_refused`, `tests/test_cds_clients.py::test_a_token_from_the_future_or_living_too_long_is_refused`, `tests/test_cds_clients.py::test_a_replayed_jti_is_refused_and_a_jti_is_per_client`, `tests/test_cds_clients.py::test_an_unregistered_jku_is_refused` | A12.1a | 23b: `server/cds_clients.py` (the registry `MENDARI_CDS_CLIENTS`, the key cache and `verify`), RS384 and ES384, each refusal the one `401` with its reason in the security log |
| CT-3b | 3 | A verified token is a narrow `cds` principal in the registered organization, holding only discover and decide, and reaching `/cds-services` only. | missing | met | `tests/test_cds_auth.py::test_a_cds_client_reaches_nothing_but_cds_services`, `tests/test_cds_clients.py::test_the_principal_is_confined_to_cds_services`, `tests/test_cds_auth.py::test_a_cds_client_decides_in_its_registered_organization`, `tests/test_auth.py::test_every_route_principal_role_and_scope_gets_what_it_should` | A12.1b | 23b: kind `cds` (`cds:<id>`, the actor `cds <id>`), holding `discover` and `decide`, refused anywhere else (`cds_only`), and the route matrix extended with it |
| CT-3c | 3 | The same registered client reads discovery and calls a hook. | partial | met | `tests/test_cds_auth.py::test_a_valid_token_reads_discovery_and_calls_the_hook` | A12.1c | 23b: a registered EHR's own tokens read discovery and call a hook, a new token for each request since a `jti` is used once (before, only a Mendari key did) |
| CT-4a | 4 | An outside CDS client (Inferno's Da Vinci CRD kit) calls over HTTP with signed tokens in CI, and Mendari verifies them. | missing | partial | `tests/test_inferno_setup.py::test_the_job_registers_infernos_issuer_and_holds_no_secret`, `tests/test_inferno_check.py::test_the_scripted_jobs_run_what_their_expected_results_list`, `tests/test_workflow_safety.py::test_conformance_runs_weekly_or_by_hand_with_no_secret_and_commits_nothing` | A12.acc-2 | 23l: `sandbox/inferno/crd_server_v221_expected.json` (1.02 to 1.06, 2.1.01, 3.4.1.01, 4.x.1.01); Inferno registered in `MENDARI_CDS_CLIENTS` of the signed-in engine (`sandbox/inferno/scripted/docker-compose.yml`); in two local runs (2026-11-06) its signed discovery and every hook call were verified (`cds:inferno-crd`, organization `pilot`), and the job `crd-server` pins each result. Partial: `conformance.yml` has not run on GitHub yet; met when its `crd-server` job's first run on main passes |
| CT-5a | 5 | Epic's own sandbox token is verified and accepted. | missing | missing | missing | A12.goal | the owner: Epic's sandbox registration, and its token's `iss`, `jku` and `aud` noted back (`notes/epic-sandbox-checklist.md`) |

**Grade:** level 2 before, level 3 after. **Overall:** level 1 before, level 1 after. Rubric v1;
before graded at `06d5332f` with LP-23a, after at `d2ef0201` with LP-23m (the close-out commit);
synthetic data and test keys only, not a vendor certification.

## 2. Who may call from where

Which hosts and browser origins may reach the CDS services, with sign-in on and off.

| Id | Level | Criterion | Before | After | Evidence | Source | Built in, or waits on |
|---|---|---|---|---|---|---|---|
| WC-2a | 2 | A request from another site reaches only the public routes and pages. | met | met | `tests/test_origin.py::test_another_sites_requests_reach_exactly_the_public_routes_and_pages` | A12.1d | |
| WC-2b | 2 | With sign-in off (open mode), `/cds-services` answers only on this machine or under an explicit sandbox flag. | missing | met | `tests/test_cds_open_mode.py::test_open_mode_answers_cds_hooks_on_this_machine_only`, `tests/test_cds_open_mode.py::test_a_spoofed_host_or_forwarded_header_is_not_this_machine`, `tests/test_cds_open_mode.py::test_the_sandbox_flag_answers_from_anywhere_and_stops_startup_on_vercel_or_with_sign_in`, `tests/test_cds_open_mode.py::test_a_verified_token_does_not_lift_the_rule_in_open_mode` | A12.1e | 23c: a loopback address, a local `Host` and no forwarding header, else `403` (`open_mode_remote`); `MENDARI_CDS_SANDBOX=1` refused on Vercel and with sign-in on (open mode answered any host before, a forged token included until 23b) |
| WC-3a | 3 | CORS on `/cds-services` and its children only, for registered EHR origins, without credentials. | missing | met | `tests/test_cds_cors.py::test_a_registered_origin_gets_cors_on_cds_services_only`, `tests/test_cds_cors.py::test_a_preflight_is_answered_before_the_guard_without_credentials`, `tests/test_cds_cors.py::test_an_unregistered_origin_gets_no_cors_and_is_refused_as_before`, `tests/browser/test_cds_cors.py::test_a_page_on_a_registered_origin_reads_a_hook_answer` | A12.1d | 23c: `server/cors.py`, the clients' `origins` |
| WC-3b | 3 | A cross-site `Origin` on a CDS client's token must be one of that client's own origins. | missing | met | `tests/test_cds_cors.py::test_an_origin_registered_for_another_client_is_refused`, `tests/test_cds_cors.py::test_an_unregistered_origin_gets_no_cors_and_is_refused_as_before` | A12.1d | 23c: `CdsRegistry.from_its_origin`, a `403` (`cds_origin`) before the `jti` is claimed |
| WC-4a | 4 | Over HTTP from another container in CI, a signed CDS client is answered by the signed-in engine, and only the sandbox's flagged engine answers in open mode. | missing | partial | `tests/test_cds_client_sandbox.py::test_ci_runs_the_signed_client_against_both_engines`, `tests/test_cds_client_sandbox.py::test_both_compose_engines_register_the_sandbox_client`, `tests/test_deploy.py::test_only_the_sandbox_engine_answers_cds_hooks_from_the_network` | A12.5a | 23e: CI's `sandbox-e2e` runs the compose service `cds-client` from its own container against the open engine (the one flagged, by 23c) and the signed-in one, each registering it; the in-process run of the same client is `tests/test_cds_client_sandbox.py::test_the_sandbox_client_signs_what_the_engine_verifies`. Partial: `sandbox-e2e` has not run on GitHub yet with the service; met when it passes on the LP-23 lane's pull request |
| WC-5a | 5 | Epic's sandbox reaches Mendari's CDS services from Epic's own servers. | missing | missing | missing | A12.acc-3 | the owner: whether Epic's free sandbox can call an external CDS service at all (owner decision 15) |

**Grade:** level 1 before, level 3 after. **Overall:** level 1 before, level 1 after. Rubric v1;
before graded at `06d5332f` with LP-23a, after at `d2ef0201` with LP-23m (the close-out commit);
synthetic data and test keys only, not a vendor certification.

## 3. Failure behaviour

What a hook does when the request is wrong, or the record cannot be read.

| Id | Level | Criterion | Before | After | Evidence | Source | Built in, or waits on |
|---|---|---|---|---|---|---|---|
| FB-2a | 2 | A request fault (a malformed CDS request, a `fhirServer` off the allow-list) is a `4xx`, and nothing is fetched. | met | met | `tests/test_fhir_client.py::test_a_malformed_cds_request_is_a_client_error`, `tests/test_fhir_client.py::test_a_server_off_the_list_is_refused` | A12.ctx-fhirAuthorization | |
| FB-2b | 2 | Input that is not FHIR R4 JSON (an unknown element or resource type, a wrong JSON type, a malformed primitive) is refused before any provider runs, in every mode. | met | met | `tests/test_fhir_structure.py::test_a_malformed_primitive_is_a_structure_error_in_every_mode` | A12.3f | |
| FB-2c | 2 | A failed read fails closed, and never echoes the server's answer. | met | met | `tests/test_fhir_client.py::test_failures_fail_closed_without_echoing_the_server` | A12.3f | |
| FB-2d | 2 | Input that is R4 JSON but breaks R4's other rules (a complex element R4 requires, a code outside an R4 required value set) is refused before any provider runs. | partial | partial | `tests/test_fhir_structure.py::test_base_r4_findings_follow_fhir_validation`, `tests/test_us_core.py::test_the_setting`, `tests/test_us_core.py::test_the_server_reports_the_mode_and_returns_the_issues` | A12.3f | the owner: the pilot's `MENDARI_FHIR_VALIDATION`. Only `enforce` blocks such input; under `warn`, the default, it is decided as if it conformed, with its issues recorded on the decision and audited, and `off` skips the check |
| FB-3a | 3 | A failed prefetch read gives a recorded ABSTAIN with its reason, and the hook still answers. | missing | met | `tests/test_cds_unreadable.py::test_a_fetch_failure_gives_abstain_with_a_reason_and_a_recorded_decision`, `tests/test_cds_unreadable.py::test_a_search_page_that_is_not_r4_abstains_never_a_500`, `tests/test_cds_unreadable.py::test_smart_decide_abstains_on_a_failed_prefetch_read` | A12.3f | 23d: each key is read on its own, and one that fails abstains with a fixed reason (it was a `422`, with nothing recorded) |
| FB-3b | 3 | Data present but unreadable (a note that is only a link, or whose data does not decode) gives ABSTAIN with its reason. | missing | met | `tests/test_cds_unreadable.py::test_a_note_that_is_only_a_link_or_does_not_decode_abstains` | A12.3a | 23d: such a note abstains (`not_fetched`, `undecodable`); before, it read as no notes |
| FB-3c | 3 | Every FHIR decision carries its data-coverage record: for each mapped field, what was looked for and what was found, as counts and declared codes. | missing | met | `tests/test_data_coverage.py::test_every_fhir_decision_carries_its_data_coverage`, `tests/test_data_coverage.py::test_no_note_text_patient_code_or_id_reaches_the_record_or_the_chain` | LP-23 requirement 7 | 23d: `data_coverage`, `mendari.data-coverage/1` |
| FB-4a | 4 | Inferno's CRD kit passes its technical-issues group: a hook whose reads fail answers, with `indeterminate` coverage for a technical reason. | missing | partial | `tests/test_inferno_check.py::test_the_scripted_jobs_run_what_their_expected_results_list`, `tests/test_inferno_setup.py::test_every_expected_failure_or_skip_has_a_reason` | A12.acc-1 | 23l: `sandbox/inferno/crd_server_v221_expected.json` (4.1): with Inferno's FHIR server failing, the answer's coverage is `indeterminate` for a `technical` reason with its text (4.1.3.02 and 4.1.3.03 pass locally, the Patient and Coverage prefetched as the kit's own example sends them). Partial: 4.1.3.01 fails on the cards' `mendari.decision_id` extension, which CRD's response model does not define; and `conformance.yml` has not run on GitHub yet, its `crd-server` job's first run on main is the CI evidence |
| FB-5a | 5 | On Epic's sandbox, a read that fails or a note Mendari cannot read abstains with its reason, and the data-coverage counts show how often. | missing | missing | missing | A12.acc-3 | the owner: the Epic sandbox run |

**Grade:** level 1 before, level 1 after. **Overall:** level 1 before, level 1 after. Rubric v1;
before graded at `06d5332f` with LP-23a, after at `d2ef0201` with LP-23m (the close-out commit);
synthetic data and test keys only, not a vendor certification.

## 4. Data shapes

Whether the mappings read the shapes an EHR sends.

| Id | Level | Criterion | Before | After | Evidence | Source | Built in, or waits on |
|---|---|---|---|---|---|---|---|
| DS-2a | 2 | Each mapped pack decides its synthetic FHIR cases as pinned (a golden of today's decisions and input hashes). | met | met | `tests/test_fhir_decisions.py::test_every_mapped_pack_decides_its_synthetic_fhir_as_the_golden_says` | A12.ctx-prefetch | 23a: the golden and its test |
| DS-2b | 2 | A synthetic FHIR record decides as the same case's JSON input does (referral, trial and prior authorization), but for a prior-authorization order the pack has no requirement set for (FX901 to FX903): through FHIR it gets no decision (no order passes the mapping's `focus_where`, so the input is refused as missing its ServiceRequest), where its JSON record decides HUMAN_REVIEW, as service `other`. | met | met | `tests/test_sandbox.py::test_referral_cases_decide_the_same_from_fhir`, `tests/test_fhir_decisions.py::test_the_new_synthetic_fhir_decides_as_its_record_does` | A12.acc-3 | 23a: the trial and prior-authorization records and their test |
| DS-2c | 2 | Prefetch search Bundles are flattened and deduplicated, and a key the EHR left out is fetched from an allow-listed `fhirServer` with the EHR's token. | met | met | `tests/test_fhir_lists.py::test_prefetch_search_bundles_are_flattened_and_deduplicated`, `tests/test_fhir_client.py::test_cds_hooks_fetches_missing_prefetch_from_an_allowed_server` | A12.ctx-prefetch | |
| DS-3a | 3 | A coding is chosen by its code system, wherever it sits. | missing | missing | missing | A12.3b | 23k: since 23g the engine chooses a coding by its system (spec 0.3's `[system=<uri>]`), and no shipped pack is on spec 0.3 until 23k; a 0.2 mapping reads the first coding only (`tests/test_trial_pack.py::test_through_fhir_a_condition_is_read_by_its_first_coding_only`) |
| DS-3b | 3 | A note linked by `Binary` is read through the allow-listed client. | missing | missing | missing | A12.3a | 23k: since 23h the engine reads a spec 0.3 note's Binary from the request's `fhirServer`, and no shipped pack is on 0.3 until 23k |
| DS-3c | 3 | The payer is read by its Coverage reference. | missing | missing | missing | A12.3c | 23k: since 23h the engine reads a spec 0.3 `follow` of `Coverage.payor` from the record or the request's `fhirServer`, and no shipped pack is on 0.3 until 23k; how a payer field reads a site's payer identifier is 23k's too |
| DS-3d | 3 | MedicationRequest is read, with a stated rule for its start and stop. | missing | missing | missing | A12.3d | 23k, and the owner: since 23g spec 0.3 states the rule (`medication_period`), and no shipped pack adopts it until 23k, which waits on the owner's go-ahead with a clinician's note on the rule (owner decision 5) |
| DS-3e | 3 | A site's own codes are translated by a per-organization site code map, versioned and hashed into each decision. | missing | met | `tests/test_site_codes.py::test_each_decision_records_the_maps_version_and_digest_for_its_organization`, `tests/test_site_codes.py::test_another_organizations_map_is_never_applied`, `tests/test_site_codes.py::test_a_decision_made_through_a_site_code_is_never_automated` | A12.3e | 23i: `MENDARI_SITE_CODES`, checked at startup against the packs' code sets, each FHIR decision recording its organization's map by version and SHA-256, and a decision read through a translation never automated (HUMAN_REVIEW until G5-6 `site-validation`). No reference pack declares a code set until 23k |
| DS-4a | 4 | A vendor-shaped synthetic profile (linked notes, several codings, MedicationRequest) passes the HL7 validator against US Core 6.1.0 in CI, and every pack with a standard hook decides on it without a failed hook. | missing | partial | `tests/test_ehr_profile.py::test_every_reference_pack_with_a_standard_hook_decides_on_the_vendor_shaped_patients_without_a_failed_hook`, `tests/test_ehr_profile.py::test_the_profile_has_linked_notes_several_codings_and_medication_requests`, `tests/test_ehr_profile.py::test_the_profile_carries_no_snomed_ct`, `tests/test_fhir_structure.py::test_the_samples_are_clean_for_both` | LP-23 requirement 10 | 23e: the profile (`sandbox/ehr_profile.py`), which `scripts/sandbox_fhir_sample.py` writes as `sandbox-ehr-profile.json` for the HL7 validator in CI's `fhir-conformance`. Every hook answers `200`, and the trial and prior-authorization packs decide on every patient (an ABSTAIN on a linked note, recorded with its reason). Partial on two counts: the referral router makes no decision at all for the three patients whose site coding comes first (ehr-ref-1, ehr-ref-3, ehr-ref-5: no record, no card, no abstention), since it finds a referral by its category's first coding, which 23k changes when the router adopts 23g's selector by code system; and `fhir-conformance` has not run on GitHub yet with the profile (the lane's pull request runs it). Each gap is pinned with why in `tests/test_ehr_profile.py::test_what_each_pack_cannot_read_yet_is_pinned` (a regression fixture, not a rate) |
| DS-5a | 5 | Every reference pack with a standard hook decides on Epic's sandbox patients without a failed hook. | missing | missing | missing | A12.acc-3 | the owner: the Epic sandbox run |

**Grade:** level 2 before, level 2 after. **Overall:** level 1 before, level 1 after. Rubric v1;
before graded at `06d5332f` with LP-23a, after at `d2ef0201` with LP-23m (the close-out commit);
synthetic data and test keys only, not a vendor certification.

## 5. Cards and workflow

What the clinician sees, and what comes back from them.

| Id | Level | Criterion | Before | After | Evidence | Source | Built in, or waits on |
|---|---|---|---|---|---|---|---|
| CW-2a | 2 | Discovery and every card say the pack's status and the notice. | met | met | `tests/test_pack_status.py::test_cds_hooks_say_it_in_discovery_and_on_every_card`, `tests/test_notice.py::test_cds_hooks_carry_it` | A12.2c | |
| CW-2b | 2 | The trial, prior-authorization and referral cards use their pack's own plain-language summaries. | met | met | `tests/test_trial_pack.py::test_patient_view_card`, `tests/test_prior_auth_pack.py::test_order_sign_gives_a_card_per_covered_order`, `tests/test_referral_pack.py::test_order_sign_gives_one_card_per_referral` | A12.2c | |
| CW-2c | 2 | A pack can decide and audit without showing a card (`show`), as the trial pack does for a patient who is clearly not eligible. | met | met | `tests/test_fhir_lists.py::test_patient_view_cards_use_the_packs_wording`, `tests/test_trial_pack.py::test_patient_view_card` | A12.2e | |
| CW-2d | 2 | No card shows a bare percentage. | missing | met | `tests/test_cds_cards.py::test_no_card_shows_a_bare_percentage` | Definition of shipped §3 | 23f: the confidence, shown as a bare percentage before, left the card's detail in every configuration |
| CW-3a | 3 | A card with suggestions says its `selectionBehavior`. | missing | met | `tests/test_cds_cards.py::test_a_card_with_suggestions_says_its_selection_behavior` | A12.2a | 23f: `at-most-one`, with the override reasons of Mendari's own CodeSystem |
| CW-3b | 3 | A feedback endpoint records accepted and overridden cards in the chain. | missing | met | `tests/test_cds_feedback.py::test_feedback_for_an_accepted_and_an_overridden_card_is_recorded_and_chained`, `tests/test_cds_feedback.py::test_feedback_naming_another_organizations_packs_or_callers_decision_answers_as_unknown`, `tests/test_ehr_pilot_e2e.py::test_a_signed_hook_its_card_and_its_feedback_verify_end_to_end` | A12.2b | 23f: `POST /cds-services/{id}/feedback`, chained as `card.feedback`, codes and ids only; 23m's end-to-end test sends it with a signed token and verifies the chain |
| CW-3c | 3 | A card's words use the pack's display names, and the engine's detail sits behind a link to the audit view. | missing | met | `tests/test_cds_cards.py::test_card_summaries_use_display_names_and_task_text_is_unchanged`, `tests/test_cds_cards.py::test_the_engine_trace_is_behind_the_audit_link_only_when_one_can_be_made` | A12.2c-audit-link | 23f: the link when `MENDARI_PUBLIC_URL` is set; without it, the reasons stay in the detail, but for the confidence thresholds' |
| CW-3d | 3 | A card links to the EHR launch view. | missing | met | `tests/test_cds_cards.py::test_a_card_links_to_the_launch_view_only_for_a_registered_ehr_of_the_same_organization` | A12.2d | 23f: a `smart` link with `?pack=`, when SMART is on and the `fhirServer` is a launch-registered EHR of the caller's organization |
| CW-3e | 3 | Referral cards show only when action is needed, and the inbox pack has a card template. | partial | partial | `tests/test_trial_pack.py::test_patient_view_card` | A12.2e, A12.2c | 23j: the mechanism exists, and the referral and inbox packs do not use it yet; the owner confirms the referral `show` list (owner decision 4) |
| CW-4a | 4 | Every card matches the CDS Hooks 2.0 card shape, and Inferno's CRD card tests pass. | missing | partial | `tests/test_inferno_check.py::test_the_scripted_jobs_run_what_their_expected_results_list`, `tests/test_inferno_setup.py::test_every_expected_failure_or_skip_has_a_reason` | A12.acc-1 | 23l: Inferno's CRD card tests run (`sandbox/inferno/crd_server_v221_expected.json`, 2.3.01 and 3.4.3.01 to 3.4.3.15): coverage information, unknown configuration, context and elements, and the coverage-info off switch pass locally; partial: response validation fails on the cards' `mendari.decision_id` extension (not in CRD's response model), and the External Reference test on the audit link card's suggestion (a Task with no id), and `conformance.yml` has not run on GitHub yet. 23f checks every card against CDS Hooks 2.0's card shape in Mendari's own tests, and a CRD service's cards carry `source.topic` |
| CW-5a | 5 | Epic's sandbox shows the cards and sends feedback on them. | missing | missing | missing | A12.2b | the owner: the Epic sandbox run, and whether Epic sends feedback (`notes/epic-sandbox-checklist.md`) |

**Grade:** level 1 before, level 2 after. **Overall:** level 1 before, level 1 after. Rubric v1;
before graded at `06d5332f` with LP-23a, after at `d2ef0201` with LP-23m (the close-out commit);
synthetic data and test keys only, not a vendor certification.

## 6. Conformance evidence

What outside tools have checked, in CI.

| Id | Level | Criterion | Before | After | Evidence | Source | Built in, or waits on |
|---|---|---|---|---|---|---|---|
| CE-2a | 2 | The HL7 validator runs offline in CI over Mendari's outputs and the sandbox's inputs (the `fhir-conformance` job), over the same files the in-process US Core checks pass. | met | met | `tests/test_ehr_baseline.py::test_ci_runs_the_hl7_validator_offline_over_mendaris_outputs_and_the_sandboxs_inputs` | A12.5d | 23a: the test (the job is `06d5332f`'s, and has run on main since) |
| CE-2b | 2 | The SMART app runs against a third-party launcher (smart-launcher-v2) in CI (the `sandbox-e2e` job). | met | met | `tests/test_ehr_baseline.py::test_ci_runs_the_smart_app_against_smart_launcher_v2` | A12.5b | 23a: the test (the job is `06d5332f`'s, and has run on main since) |
| CE-3a | 3 | A CDS Hooks client calls the services over HTTP with signed tokens, in the sandbox and in CI. | missing | partial | `tests/test_cds_client_sandbox.py::test_the_sandbox_client_signs_what_the_engine_verifies`, `tests/test_cds_client_sandbox.py::test_it_keeps_its_key_in_memory_only`, `tests/test_cds_client_sandbox.py::test_ci_runs_the_signed_client_against_both_engines` | A12.5a | 23e: `mendari sandbox cds-client` (`sandbox/cds_client.py`), an ES384 key made in memory and its JWK Set served, run by the compose service `cds-client` in CI's `sandbox-e2e`. Partial: `sandbox-e2e` has not run on GitHub yet with the service; met when it passes on the LP-23 lane's pull request |
| CE-4a | 4 | Inferno's Da Vinci CRD kit runs as the CDS client in CI, with each test's outcome pinned with its reason. | missing | partial | `tests/test_inferno_setup.py::test_every_expected_failure_or_skip_has_a_reason`, `tests/test_inferno_check.py::test_the_scripted_jobs_run_what_their_expected_results_list`, `tests/test_workflow_safety.py::test_conformance_runs_weekly_or_by_hand_with_no_secret_and_commits_nothing` | A12.acc-1, A12.acc-2 | 23l: `conformance.yml` job `crd-server` (`sandbox/inferno/crd_server_v221_expected.json`), suite `crd_server_v221` groups 1, 2, 3.4 and 4 (3.7 not started: its must-support test names no requirement), each result pinned with its reason and requirement ids from a local run and confirmed by a second. Partial: `conformance.yml` has not run on GitHub yet; met when its `crd-server` job's first run on main passes |
| CE-4b | 4 | Inferno's SMART App Launch and Bulk Data client suites run for Mendari's roles. | missing | partial | `tests/test_inferno_check.py::test_the_scripted_jobs_run_what_their_expected_results_list`, `tests/test_inferno_session.py::test_a_smart_launch_runs_from_the_launch_url_inferno_made_then_resumes_the_test`, `tests/test_inferno_session.py::test_a_bulk_export_signs_in_with_the_runs_key_given_by_environment_only`, `tests/test_bulk.py::test_a_finished_export_is_deleted_on_the_server_once_every_file_is_here` | A12.5b, A12.5c | 23l: jobs `smart-client` (`sandbox/inferno/smart_client_stu2_2_expected.json`, a public client's EHR launch: every test passes but 3.01, which asks https) and `bulk-client` (`sandbox/inferno/bulk_data_v200_client_expected.json`, every test passes, the delete request included), each pinned from a local run and confirmed by a second. Partial: `conformance.yml` has not run on GitHub yet; met when the first run on main of both jobs passes |
| CE-4c | 4 | The HL7 validator also runs with a terminology server, on a schedule. | missing | partial | `tests/test_workflow_safety.py::test_terminology_validation_runs_on_a_schedule_and_never_on_a_pull_request`, `tests/test_fhir_packages.py::test_offline_is_the_default_and_tx_is_opt_in`, `tests/test_fhir_packages.py::test_tx_fails_when_the_validator_put_a_package_the_run_lacks_in_its_cache` | A12.5d | 23l: `.github/workflows/terminology.yml`, weekly and by hand, runs `fhir-conformance`'s steps (the same validator, locked packages and generated inputs, pinned step for step) with `scripts/fhir_validate.py --tx https://tx.fhir.org/r4` in place of `--offline`; the offline job stays the required gate (CE-2a). Partial: `terminology.yml` has not run on GitHub yet, and never from a build session; met when its first run on main passes (which issues tx.fhir.org raises, and so which expected warnings it needs, is known only then) |
| CE-4d | 4 | The scopes Mendari asks for are checked against the scopes the EHR publishes (`scopes_supported`). | missing | partial | `tests/test_smart_check.py::test_a_scope_the_ehr_does_not_publish_fails`, `tests/test_smart_check.py::test_unpublished_scopes_are_unknown_not_passed`, `tests/test_smart_check.py::test_a_v1_wildcard_scope_covers_its_v2_form`, `tests/test_smart_check.py::test_smart_launcher_v2_s_published_scopes_cover_what_the_sandbox_asks`, `tests/test_ehr_baseline.py::test_ci_runs_the_smart_app_against_smart_launcher_v2` | A12.5e | 23l: `mendari smart check` (SMART on) compares each scope asked with `scopes_supported`, SMART v1 and v2 alike; one not published is `fail` (plan decision 56; whether it should be `warn`, as SMART 2.2.0's "not exhaustive" suggests, is the owner's question), no list is `unknown`, never `ok`. `sandbox-e2e` runs it against smart-launcher-v2, whose pinned image publishes `patient/*.*`. Partial: `sandbox-e2e` has not run on GitHub yet with the scope check; met when it passes on the LP-23 lane's pull request |
| CE-5a | 5 | `mendari smart check` and the scope check pass against Epic. | missing | missing | missing | A12.5e | the owner: `mendari smart check` run against Epic's FHIR base |

**Grade:** level 2 before, level 2 after. **Overall:** level 1 before, level 1 after. Rubric v1;
before graded at `06d5332f` with LP-23a, after at `d2ef0201` with LP-23m (the close-out commit);
synthetic data and test keys only, not a vendor certification.

## 7. Vendor fit

Session length, embedding and the launch's form, as the named vendor needs them.

| Id | Level | Criterion | Before | After | Evidence | Source | Built in, or waits on |
|---|---|---|---|---|---|---|---|
| VF-2a | 2 | A launch ends with the EHR's token: `offline_access` is refused, so no refresh token is kept. | met | met | `tests/test_smart_sealing.py::test_offline_access_is_refused_at_launch` | A12.4 | |
| VF-2b | 2 | Inside an iframe the app fails safely (`frame-ancestors 'none'` on every response). | met | met | `tests/test_security_headers.py::test_every_response_carries_the_headers` | A12.4 | |
| VF-2c | 2 | A SMART launch with `?pack=` opens the patient's view, in a new window or the EHR's embedded window. | met | met | `tests/test_smart.py::test_an_ehr_launch_opens_the_patients_view_with_a_server_side_session` | A12.2d | |
| VF-3a | 3 | A session lasts as long as the vendor needs (refresh tokens, if Epic needs them). | missing | missing | missing | A12.4 | the owner: the Epic sandbox's token lifetime, and whether Epic needs refresh tokens (owner decision 13) |
| VF-3b | 3 | The app embeds as the vendor needs (in an iframe, if Epic needs one). | missing | missing | missing | A12.4 | the owner: how Epic's sandbox opens the app (owner decision 13) |
| VF-3c | 3 | The launch URL is in the form the vendor registers (`?pack=` or `appContext`). | partial | partial | `tests/test_smart.py::test_an_ehr_launch_opens_the_patients_view_with_a_server_side_session` | A12.2d | the owner: whether Epic keeps the `?pack=` query (owner decision 12) |
| VF-4a | 4 | Inferno's SMART App Launch client suite passes for the vendor's launch form. | missing | partial | `tests/test_inferno_check.py::test_the_scripted_jobs_run_what_their_expected_results_list`, `tests/test_inferno_session.py::test_a_smart_launch_runs_from_the_launch_url_inferno_made_then_resumes_the_test` | A12.5b | 23l: `sandbox/inferno/smart_client_stu2_2_expected.json`; the public client's EHR launch (5.03, 5.07, 5.10, 5.12) passes locally; partial: 3.01 fails over plain http (the deployment's TLS), the sign-in profile (`openid fhirUser`) is not run against Inferno (its id_token has no nonce and its configuration no jwks_uri, which Mendari refuses), whether that is Epic's launch form waits on the owner's sandbox notes, and `conformance.yml` has not run on GitHub yet, its `smart-client` job's first run on main is the CI evidence |
| VF-5a | 5 | A launch and a hook from Epic's sandbox work end to end. | missing | missing | missing | A12.acc-3 | the owner: the Epic sandbox run |

**Grade:** level 2 before, level 2 after. **Overall:** level 1 before, level 1 after. Rubric v1;
before graded at `06d5332f` with LP-23a, after at `d2ef0201` with LP-23m (the close-out commit);
synthetic data and test keys only, not a vendor certification.
