<!-- Planned by a mapping workflow on 2026-10-17 against main 06d5332f. A reader mapped the code
LP-23 builds on: CDS Hooks, the auth guard and scopes, the JWT pieces that can be reused, SMART,
outbound FHIR, the mapping language, the decision record, the sandbox, CI and the Inferno kits.
Two planners split the work into pull requests, one putting safety first and one cutting thin
slices. A judge checked their claims against the code and wrote this plan from both. The
conflicts below are the defaults each PR follows unless the owner chooses otherwise.
tasks/todo.md tracks progress. -->

# LP-23 build plan: EHR pilot readiness (A12)

> **Numbers allocated across the lanes built together on 2026-10-17** (so parallel PRs never take the
> same id; `tests/test_threat_model.py` still checks the order): ADR-0013 is LP-17's and ADR-0014
> LP-23's. Threat-model rows: LP-18h takes S18, S19, T7, R3, D11, D12, E12 and E13; LP-17 takes
> S20, T8, R4, I14, I15 and E14; LP-23 takes S21, T9, R5, I16, I17, I18, D13, D14, E15 and E16.

## Summary

LP-23 lets a real EHR call Mendari's CDS Hooks services with its own signed token. Mendari then
does one of two things:
- it reads EHR-shaped data and decides;
- or it records what it could not read, abstains, and still answers.

LP-23 delivers:
- A registry of CDS clients. Each EHR's issuer is bound to its keys (by value, or one pinned
  `jwks_uri` on the allow-list), an organization and its allowed `fhirServer` bases.
- A verifier for RS384 and ES384 tokens: signature, iss, aud, exp, iat, a `jti` replay check,
  and `jku` only when it equals the registered URI.
- A `cds` principal confined to `/cds-services`.
- Open mode that answers CDS Hooks only on this machine or under a sandbox flag.
- CORS for registered origins, without credentials.
- A data-coverage record on every FHIR decision. A read that fails, or data present but
  unreadable, gives ABSTAIN with a reason, and the hook still answers.
- Cards as CDS Hooks 2.0 and Da Vinci CRD expect, and a chained feedback endpoint.
- Mapping syntax 0.3: a selector that picks a coding by its code system, fallback paths, lists
  that read several resource types, MedicationRequest with a stated rule for start and stop,
  references followed inside the record, and code sets.
- Linked notes, payer and medication read through the allow-listed client.
- A site code map per organization, versioned and hashed into each decision.
- A vendor-shaped synthetic profile, and a signed sandbox CDS client over HTTP.
- The Inferno Da Vinci CRD kit as the CDS client in CI, plus the SMART App Launch and Bulk Data
  client suites.
- The HL7 validator with a terminology server, on a schedule.
- A check of the scopes Mendari asks for against the scopes the vendor publishes.
- The EHR scorecard and its rubric, committed and graded.

It is built in **13 PRs, 23a to 23m**, in this order:
1. The plan and an honest baseline.
2. The guards for who may call: tokens, the principal, open mode and CORS.
3. Failure behaviour and the data-coverage record.
4. The first end-to-end result over HTTP.
5. Cards and feedback.
6. Mapping syntax, linked reads and the site code map, all off for shipped packs.
7. Pack adoption, which needs the owner's go-ahead.
8. Conformance CI.
9. The close-out.

Order of work:
- 23a comes first.
- **In parallel once 23a is merged:** 23b, 23d and 23g.
- 23c follows 23b. 23f needs 23b and 23d. 23e needs 23b, 23c and 23d.
- **In parallel:** 23h and 23i. Both need 23d and 23g.
- 23j follows 23f. 23k needs 23e, 23h and 23i.
- 23l needs 23b and 23d, and can be built beside 23e to 23k.
- 23m closes LP-23.

Where PRs collide, merge in this order:

| File | Merge order |
|---|---|
| `server/auth.py` | 23b, then 23c, then 23f |
| `standards/cds_hooks.py` | 23d, then 23f, then 23h |
| `core/decision.py` | 23d, then 23i |
| `standards/fhir.py` | 23g, then 23h and 23i |
| `sandbox/inferno/*_expected.json` | 23l first; 23f and 23k each refresh it |

These stay off until the owner acts:
- With `MENDARI_CDS_CLIENTS` unset, no JWT is accepted anywhere. With sign-in on, behaviour is
  exactly as today.
- With `MENDARI_SITE_CODES` unset, no code is translated.
- `MENDARI_CDS_SANDBOX` is set only by the compose sandbox engine and the test suite.
- No shipped pack declares spec 0.3 until 23k, which needs the owner's go-ahead.
- Production gets none of these settings until a pilot is signed.

The plan adds **no database table**:
- The registry and the site code map are settings checked at startup.
- Card feedback is a decision event.
- `jti` claims use `smart_sessions`.

Figures:
- LP-23 computes no statistic, and nothing in it ranks.
- **Scorecard:** it shows rubric levels. Each level carries its scope: rubric version, commit,
  "synthetic data and test keys only, not a vendor certification". It also says that a rubric
  level is an assessment, not an estimate from a sample, so it has no interval.
- **Data coverage:** counts per field, for one decision.
- **Inferno results:** each test's own outcome, with the kit version, suite and commit, never a
  pass rate.
- **Any rate added later** (unreadable decisions in health, for example) appears as k of n with
  its Clopper-Pearson interval (`evals/stats.proportion`), its organization and its time window.

Every PR:
- passes the Definition of shipped (`notes/vision-audit.md:1310`);
- updates `README.md`, `CHANGELOG.md`, the LP-23 entry in `tasks/todo.md` and the docs it names;
- puts the SPDX line on new source files;
- signs off every commit (`git commit -s`);
- makes its test keys at runtime and commits none;
- runs no live provider in CI.

## Pull requests, in order

- [ ] **23a** Plan entry, ADR-0014 (Proposed), the Epic sandbox checklist, the EHR scorecard graded at main, and a golden of today's FHIR decisions.
  - **What it builds:**
    - `notes/lp23-plan.md` (this plan). The `tasks/todo.md` LP-23 entry holds the conflicts and
      owner decisions below, written before any build, as the prompt requires.
    - `docs/decisions/ADR-0014-a-cds-client-is-a-principal.md` (Proposed). It records:
      - the registry and the token rules;
      - the `cds` kind, confined to `/cds-services`;
      - verification in every mode;
      - open mode answering on this machine only;
      - CORS;
      - ABSTAIN on unreadable data, and the data-coverage record.

      It amends ADR-0006's rule that the narrow `ehr` principal exists only for SMART launches.
      Refresh tokens and iframe mode stay deferred, as ADR-0006:51-55 already says.
    - `notes/epic-sandbox-checklist.md`, the checklist B38 says Claude sends. The owner gets it
      when 23a merges. It covers:
      - **What to register:** the discovery URL, each service URL, and Epic's JWK Set URL to put
        on the allow-list.
      - **The APIs the packs read:** Patient, Condition, Observation, MedicationRequest,
        Medication, MedicationStatement, DocumentReference, Binary, Coverage, Organization,
        ServiceRequest, Communication, Procedure and DiagnosticReport.
      - **What to note back:**
        - `iss`, `jku`, the form of `aud`, whether `sub` or `tenant` is sent, and the token
          lifetime;
        - whether feedback is sent, and which hooks and prefetch tokens are supported;
        - how the launch URL is registered (a `?pack=` query or `appContext`);
        - an embedded window or an iframe, and refresh tokens;
        - wildcard scopes and the published `scopes_supported`;
        - whether the free sandbox can call an external CDS service at all.
      - **Where credentials go:** `~/Documents/mendari-private`, never the repository.
      - Oracle Health is the backup.
    - `notes/ehr-scorecard.md`, rubric v1:
      - Seven dimensions: client trust; who may call from where; failure behaviour; data shapes;
        cards and workflow; conformance evidence; vendor fit (session length, embedding).
      - Each dimension is graded 1 to 5 by named criteria. Each criterion is marked met, partial
        or missing, with a test id or "missing".
      - It is graded at `06d5332f` from the A12 audit rows (`notes/vision-audit.md:579-614`).
      - The overall grade is the lowest dimension, shown beside every dimension, so a strong
        row cannot hide a missing one.
      - Every grade carries the scope line from the Summary.
      - It says that the external "2.5 of 5" and its rubric are not in the repository or its
        history (A12.goal), so that grade is not reproduced.
      - It shows no percentage and no fraction.
      - Each owner-dependent row says what it waits on.
    - Synthetic FHIR for the two packs that only tests build today (`tests/test_trial_pack.py`
      `_record`, `tests/test_prior_auth_pack.py` `_prefetch`): `sandbox/fhir_records.py` gains
      `trial_entries` and `prior_auth_entries`.
    - `tests/fixtures/fhir/decisions-golden.json`, written by `scripts/fhir_decisions_golden.py`.
      - It covers each synthetic FHIR case of the four mapped packs.
      - For each case it holds the outcome, the recommendation, the priority, each answer's
        label and `input_hash`. The hash is taken with no store, so it is the unkeyed
        `decision.input_hash` (`core/decision.py:671`).
      - Matching hashes prove that the adapter maps the same bytes as on main.
  - **Files:**
    - New: `notes/lp23-plan.md`, `notes/epic-sandbox-checklist.md`, `notes/ehr-scorecard.md`,
      the ADR, `scripts/fhir_decisions_golden.py`, the fixture, `tests/test_ehr_scorecard.py`,
      `tests/test_fhir_decisions.py`.
    - Changed: `src/mendari/sandbox/fhir_records.py`, `mkdocs.yml` (the ADR's nav entry),
      `tasks/todo.md`, `CHANGELOG.md`.
  - **Tests:**
    - `tests/test_ehr_scorecard.py`:
      - `::test_every_row_cites_a_test_that_exists_or_says_missing`, collect-only as
        `tests/test_gates.py::test_every_test_the_file_names_exists` does;
      - `::test_every_grade_states_its_rubric_version_commit_and_scope`;
      - `::test_the_scorecard_shows_no_percentage_or_fraction`.
    - `tests/test_fhir_decisions.py::test_every_mapped_pack_decides_its_synthetic_fhir_as_the_golden_says`.
  - **Safe alone because:** it adds docs, sandbox generators and read-only tests that pin
    today's behaviour.

- [ ] **23b** Signed CDS client tokens: the registry, the verifier and the `cds` principal (requirements 1 and 2). This PR gets one adversarial security review.
  - **What it builds:**
    - `src/mendari/server/cds_clients.py`, holding the registry, the key cache and the verifier.
    - **The registry.**
      - A frozen `CdsClient` holds: `id`, `title`, `iss`, `org`, `fhir_servers`, `origins` (used
        by 23c), either `jwks_uri` or `jwks` by value, and optionally `sub`, `tenant` and
        `iss_is_fhir_server`.
      - `cds_clients()` reads `MENDARI_CDS_CLIENTS` (a JSON list). It is checked at startup,
        next to `smart_ehrs()` (`app.py:802`). Unset means None, and the feature is off.
      - The startup check requires:
        - unique ids and unique iss values;
        - keys given by value to pass `backend_services.check_jwks`: public keys only, RSA
          2048-8192 or EC P-384, distinct kids;
        - a `jwks_uri` on https, or plain http only for a loopback or single-label host, with
          its origin on `MENDARI_FHIR_ALLOWED_SERVERS`;
        - each `fhir_servers` base to pass `smart.normalize_base` and be on the allow-list;
        - the org to match any `MENDARI_SMART_EHRS` entry for the same base;
        - every org to be `default` when sign-in is off;
        - `origins` to pass `origin.origin_of`;
        - `MENDARI_PUBLIC_URL` to be set.
      - Startup messages name the variable and the entry's position, never key material.
    - **`JwksCache`:**
      - it fetches only through `GuardedHttp` and checks every answer with `check_jwks`;
      - an answer holding a private member is refused, and nothing is cached;
      - entries live 3600 s, as `oidc.Discovery` does;
      - an unknown kid refetches at most once per client per 60 s.
    - **`verify(token, *, audience, now, clients, keys, claim) -> CdsCaller`.**
      - Shape: at most `MAX_ASSERTION_BYTES` (8 KiB, `backend_services.py:80`), three
        base64url segments.
      - Header:
        - it holds only {alg, kid, typ, jku};
        - `alg` is in `backend_services.ALGORITHMS` (RS384, ES384);
        - `typ` is `"JWT"` and `kid` is present;
        - `jku` equals the registered `jwks_uri` exactly; a client with keys by value takes no
          `jku`.
      - Key: exactly one key per kid and kty.
      - Claims:
        - `jwt.decode` runs with `strict_aud` and requires iss, aud, exp, iat and jti, with
          30 s leeway;
        - `iat` is not in the future, `exp − iat ≤ 330` and `exp ≤ now + 330`;
        - `sub` and `tenant` must equal the registered values when those are set;
        - a `tenant` claim is refused when none is registered.
      - Replay: `claim("cds_jti", f"{client}\n{jti}", exp + 30)` runs last, so a refused token
        claims nothing.
      - Each refusal reason comes from a closed set.
    - `AuditStore.smart_claim` (`core/audit.py:2792`) gains the bounded expiry purge that
      `smart_put` has (`:2665-2672`).
    - **`auth.py`.**
      - New `CDS_PATH = re.compile(r"^/cds-services(/|$)")`.
      - In `Auth.check`, a CDS branch sits above the open-mode shortcut (`auth.py:1062`), so it
        runs in both modes. On `CDS_PATH`, an Authorization header holding a JWT-shaped Bearer
        credential goes to `_cds()`:
        - without a registry, it gets 401 `cds_unregistered`;
        - otherwise it is verified with `aud = MENDARI_PUBLIC_URL + request path`, never the
          Host header.
      - A verified token gives a principal of kind `cds`:
        - id `cds:<id>` and actor `cds <id>`;
        - the registry's org, with `recorded(org)` checked in memberships mode;
        - `CDS_PERMISSIONS = {discover, decide}` through `role_permissions`.
      - A `cds` principal anywhere else gets 403 `cds_only`.
      - In open mode, a non-JWT header is still ignored, as today.
      - `_BEARER` (`:555`) and `_bearer` do not change. A JWT on any other path is still
        `malformed`.
    - **`cds_invoke` (`app.py:2442`):** a `cds` principal's `fhirServer` must normalise to one
      of the client's bases. When `iss_is_fhir_server` is set it must also equal `iss`.
      Otherwise the answer is 403 `fhir_server_not_registered`, before charging or fetching.
    - **Logs and health:**
      - the security log names refusals by `cds_<reason>` and the caller as `cds:<id>`, never a
        claim value or the token;
      - `/api/health` shows `cds_clients` ids to read principals only.
    - **Settings:** `MENDARI_CDS_CLIENTS` joins `LIVE_SETTINGS` in `tests/conftest.py`, with a
      row in `docs/deploying-with-phi.md` §8.
    - **Docs:**
      - `docs/deploying-with-phi.md` §2 (CDS clients beside the `cds-hooks` key preset) and §9;
      - a new `docs/guides/connecting-an-ehr.md`, with its nav entry;
      - `docs/guides/api.md`;
      - ADR-0014;
      - threat-model rows S21 (a forged, replayed or misaddressed CDS token, or a spoofed EHR),
        E15 (a CDS token used beyond `/cds-services`) and D13 (an unknown-kid refetch flood),
        with E1 and R2 updated;
      - the scorecard's "client trust" rows.
  - **Files:**
    - Code: `src/mendari/server/{cds_clients.py (new), auth.py, app.py}`, `src/mendari/core/audit.py`.
    - Tests: `tests/jwt_signers.py` (new). `Signer`, `assertion` and `forged` move there from
      `tests/test_backend_services.py`, which imports them back with the same keys and bytes.
      `tests/conftest.py`.
    - Docs: as listed above.
  - **Tests:**
    - `tests/test_cds_clients.py`, with keys made at runtime:
      - `test_a_valid_token_is_accepted[RS384|ES384]`, `test_a_wrong_issuer_is_refused`,
        `test_a_wrong_audience_is_refused`, `test_an_expired_token_is_refused`;
      - `test_a_replayed_jti_is_refused_and_a_jti_is_per_client` (two app instances, SQLite and
        Postgres), `test_an_unregistered_jku_is_refused`;
      - `test_a_header_outside_alg_kid_typ_jku_is_refused` (x5u, x5c, jwk, crit, typ missing),
        `test_none_hs384_and_rs256_are_refused`;
      - `test_a_token_from_the_future_or_living_too_long_is_refused`,
        `test_a_subject_or_tenant_other_than_registered_is_refused`,
        `test_a_refused_token_claims_no_jti`;
      - `test_an_unknown_kid_refetches_at_most_once_a_minute`,
        `test_a_jwks_answer_with_a_private_key_is_refused_and_nothing_is_cached`;
      - `test_the_registry_is_checked_at_startup` (each refusal is named, with no key material),
        `test_unset_means_no_cds_client`, `test_expired_claims_are_purged_in_bounded_batches`.
    - `tests/test_cds_auth.py`, over HTTP:
      - `test_a_valid_token_reads_discovery_and_calls_the_hook` (sign-in on);
      - `test_wrong_iss_wrong_aud_expired_replayed_and_unregistered_jku_are_each_refused_over_http`;
      - `test_a_cds_client_reaches_nothing_but_cds_services`, run over the route inventory;
      - `test_open_mode_refuses_an_unverified_jwt_on_cds_services`;
      - `test_a_fhir_server_the_client_did_not_register_is_refused_before_any_read`;
      - `test_a_cds_client_decides_in_its_registered_organization` (memberships mode);
      - `test_a_long_jwt_elsewhere_is_still_malformed`;
      - `test_each_refusal_is_logged_with_its_reason_and_no_token_or_claim`;
      - `test_health_shows_client_ids_to_read_principals_only`.
      - Every token test asserts that the registry is on and the principal kind is `cds`.
    - Extended with kind `cds`:
      `tests/test_auth.py::test_every_route_principal_role_and_scope_gets_what_it_should`,
      `tests/test_limits.py::test_every_principal_kind_is_capped_on_the_routes_it_decides_on`.
    - Must pass unchanged: `tests/test_key_presets.py`, `tests/test_scopes.py`, and
      `tests/test_backend_services.py`'s
      `test_a_replayed_assertion_is_refused_and_a_jti_is_per_client`,
      `test_every_assertion_that_does_not_authenticate_gets_one_answer_and_writes_nothing` and
      `test_a_jwks_with_anything_but_public_signing_keys_is_refused`.
  - **Safe alone because:**
    - With the registry unset, sign-in-on behaviour is identical: a JWT is still refused.
    - The one open-mode change is a tightening. A JWT-shaped header on `/cds-services` is now
      refused; today a forged JWT gets 200 with cards (A12.ctx-jwt-today).
    - `ohk_` keys and `mat_` tokens do not change.
    - The notice gate already exempts every kind but `user` (`app.py:2798`).

- [ ] **23c** Who may call from where: open mode answers CDS Hooks only on this machine or under the sandbox flag, and CORS serves registered origins (requirements 3 and 4).
  - **What it builds:**
    - **Open-mode confinement.** In 23b's CDS branch, with sign-in off, a request goes on only
      when it is local or `MENDARI_CDS_SANDBOX=1` is set.
      - Local means all three hold: `limits.client_ip(request)` is a loopback address,
        `origin.is_local_host(Host)` holds, and there is no `Forwarded` or `X-Forwarded-For`
        header.
      - Anything else gets 403 with fixed text, logged as `open_mode_remote`.
      - A verified token does not lift the rule.
    - **The sandbox flag:**
      - it stops startup when `VERCEL` is set or sign-in is on;
      - the `engine` service in `docker-compose.yml` sets it; `engine-signin` and
        `deploy/onprem` do not;
      - an autouse fixture in `tests/conftest.py` sets it, because Starlette's TestClient
        connects as `"testclient"` with Host `testserver`;
      - `tests/test_cds_open_mode.py` deletes it again.
    - **CORS:** `src/mendari/server/cors.py`, a middleware outside the guard, for `CDS_PATH`
      only.
      - A preflight from a registered origin gets 204 with:
        - `Access-Control-Allow-Origin` set to that exact origin;
        - Allow-Methods `GET, POST, OPTIONS`;
        - Allow-Headers `authorization, content-type`;
        - `Max-Age: 600` and `Vary: Origin`.
      - Answers to registered origins carry `Access-Control-Allow-Origin` and `Vary`.
      - `Access-Control-Allow-Credentials` is never sent.
      - An unregistered origin gets no CORS headers and is refused as before.
      - The registered origins are the union of the clients' `origins`.
    - **Origin checks:**
      - `OriginCheck` (`origin.py:66`) lets a registered origin through on `CDS_PATH` only;
      - in 23b's branch, a cross-site `Origin` on a `cds` token must be one of that client's
        own origins, else 403;
      - `Cross-Origin-Resource-Policy` stays as it is.
    - **Snippet:** `src/mendari/dashboard/snippets/cds.sh`, run by
      `scripts/check_doc_snippets.py` against a server on 127.0.0.1, which counts as local, so
      no flag is needed.
    - **Docs:** `docs/deploying-with-phi.md` §2 and §8; `connecting-an-ehr.md`; threat-model
      rows E16 (open mode answering from a network) and I16 (another site's page reading hook
      answers through CORS). The CHANGELOG names the change for open-mode deployments reached
      over a network.
  - **Files:** `src/mendari/server/{cors.py (new), auth.py, origin.py, app.py, cds_clients.py}`,
    `docker-compose.yml`, `tests/conftest.py`, the snippet, and the docs above.
  - **Tests:**
    - `tests/test_cds_open_mode.py`:
      - `test_open_mode_answers_cds_hooks_on_this_machine_only`;
      - `test_a_spoofed_host_or_forwarded_header_is_not_this_machine`;
      - `test_the_sandbox_flag_answers_from_anywhere_and_stops_startup_on_vercel_or_with_sign_in`;
      - `test_a_verified_token_does_not_lift_the_rule_in_open_mode`;
      - `test_the_rest_of_open_mode_is_unchanged`.
    - `tests/test_deploy.py::test_only_the_sandbox_engine_answers_cds_hooks_from_the_network`.
    - `tests/test_cds_cors.py`:
      - `test_a_registered_origin_gets_cors_on_cds_services_only`;
      - `test_a_preflight_is_answered_before_the_guard_without_credentials`;
      - `test_an_unregistered_origin_gets_no_cors_and_is_refused_as_before`;
      - `test_an_origin_registered_for_another_client_is_refused`.
    - `tests/browser/test_cds_cors.py::test_a_page_on_a_registered_origin_reads_a_hook_answer`.
      It proves that CORP `same-origin` does not block a CORS fetch.
    - Updated with the CDS rows:
      `tests/test_origin.py::test_another_sites_requests_reach_exactly_the_public_routes_and_pages`
      and `::test_every_route_and_kind_of_request_gets_what_it_should`.
  - **Safe alone because:**
    - It only narrows open mode, and CORS is off without registered origins.
    - With sign-in on, the only change is that preflights from registered origins are answered.
    - No CI job or script calls `/cds-services` over HTTP today (A12.5a).

- [ ] **23d** A hook that always answers: an unreadable record gives a recorded ABSTAIN with its reason, and every FHIR decision carries its data-coverage record (requirement 7).
  - **What it builds:**
    - **`src/mendari/standards/data_coverage.py`** (pure, `mendari.data-coverage/1`).
      `measure(mapping, indexed, *, unreadable)` reports, for each mapped field and list:
      - the paths looked for, the declared systems and the pack's own looked-for codes;
      - resources counted by type, and the number of values found;
      - codings counted by declared system, with every other system as one count;
      - unreadable keys as reason → count.

      The reasons are a closed set, `READ_REASONS`: `not_fetched`, `network`, `timeout`,
      `http_status`, `unauthorized`, `not_json`, `too_large`, `operation_outcome`, `not_r4`,
      `undecodable`, `unsupported_type`, `off_server`, `cap`.

      The output is canonical and sorted. It never holds a value, an id, text, a patient's code
      or an undeclared system string.
    - **`FhirError`** (`errors.py:106`) gains an additive `reason=` keyword, like `status=`. It
      is set at each raise in `fhir_client.py`.
    - **`cds_hooks.fetch`** (`:142-160`) reads each key on its own and returns
      `(found, unreadable)`.
      - A key whose prefetch value is null is "no data" and is not fetched. The rule at `:105`
        becomes `key not in prefetch`.
      - An OperationOutcome value, sent or fetched, is unreadable.
      - Resources Mendari fetched itself go through the R4 structure gate on their own. A
        failure marks that key `not_r4`.
      - What the client sent keeps the 400 gate.
    - **No fhirServer.** Keys left out with no `fhirServer` raise
      `PrefetchRequired(FhirError)`. It is answered 412, through an entry placed before
      `FhirError` in `app.py` `_STATUS` (`:150`, "the first match wins").
    - **ABSTAIN.** `decide(..., unreadable=)` and `Decision.decide_fhir(..., unreadable=)`:
      - rejected or schema-invalid input still gives BLOCK (`decision.py:447-454`);
      - otherwise any unreadable key gives ABSTAIN, before any provider runs and before
        `adapter.index` can raise for a required resource whose read failed;
      - the reasons read `could not read <key>: <reason>`;
      - the decision is recorded like any other.
    - **Notes:** a `base64_text` attachment whose data is present but does not decode is
      `undecodable`, and one with only a `url` is `not_fetched`. Both abstain. `fhir._decode`
      does not change.
    - **`DecisionResult.data_coverage`** appears in `to_dict` and `to_record`, and is null for
      JSON decisions. It is filled on CDS, `fhir/decide`, `$decide`, SMART decide and DTR.
    - **SMART decide.** `smart_routes.decide_for` (`:443-466`) uses the same per-key read. A
      failed prefetch read abstains. A failed read of the focus resource keeps today's error.
    - **The card.** An ABSTAIN over unreadable data shows a card whatever `show` says:
      - indicator `info`;
      - fixed words naming the prefetch keys and reasons (owner decision 6);
      - never a URL, a query or a server's text.
    - **CRD.** For a pack with `coverage_information` whose Coverage was read, an order that
      abstained over unreadable data gets:
      - `covered: indeterminate`, with reason `technical` and a fixed reason `text`;
      - built by `davinci.py`, while `CoverageInformation.covered` (`spec.py:593`) stays as it
        is;
      - a sample in `scripts/davinci_sample.py`, validated by `fhir-conformance` against CRD
        2.2.1.
    - **Docs:** `docs/guides/api.md`, `connecting-an-ehr.md`, threat-model row I17 (the
      coverage record or its reasons carrying values, URLs or note text), the scorecard's
      "failure behaviour" rows.
  - **Files:**
    - Code: `src/mendari/standards/{data_coverage.py (new), cds_hooks.py, fhir_client.py, fhir.py, davinci.py}`,
      `src/mendari/core/decision.py`, `src/mendari/errors.py`,
      `src/mendari/server/{app.py, smart_routes.py}`, `scripts/davinci_sample.py`.
    - Docs: as listed above.
  - **Tests:**
    - `tests/test_data_coverage.py`:
      - `test_each_mapped_field_says_what_was_looked_for_and_found_as_counts`;
      - `test_codings_are_counted_by_declared_system_and_others_only_as_a_number`;
      - `test_a_missing_list_and_an_empty_list_are_told_apart`;
      - `test_the_record_is_the_same_bytes_every_run`;
      - `test_every_fhir_decision_carries_its_data_coverage` (all five entry points);
      - `test_a_json_decision_carries_none`;
      - `test_no_note_text_patient_code_or_id_reaches_the_record_or_the_chain` (planted marks,
        under `encrypt`);
      - `test_old_records_still_verify_beside_new_ones` (SQLite and Postgres).
    - `tests/test_cds_unreadable.py`:
      - `test_a_fetch_failure_gives_abstain_with_a_reason_and_a_recorded_decision`, for each
        reason;
      - `test_the_reason_holds_no_url_query_or_server_text`;
      - `test_a_null_prefetch_value_is_no_data_and_is_not_fetched`;
      - `test_an_operation_outcome_in_prefetch_abstains`;
      - `test_no_provider_runs_when_the_record_is_unreadable`;
      - `test_rejected_input_still_blocks`;
      - `test_an_unreadable_record_never_raises_automation` (every pack × every reason);
      - `test_request_faults_stay_4xx_and_the_r4_gate_is_unchanged`;
      - `test_an_unreadable_abstain_shows_a_card_whatever_show_says`;
      - `test_a_failed_read_gives_indeterminate_technical_coverage_when_coverage_was_read`;
      - `test_smart_decide_abstains_on_a_failed_prefetch_read`.
    - `mendari audit verify` passes after an ABSTAIN, on both databases.
    - Changed: `tests/test_fhir_client.py::test_without_a_fhir_server_prefetch_is_still_required`
      goes from 422 to 412.
    - Unchanged:
      - `tests/test_fhir_client.py::test_a_server_off_the_list_is_refused` (422, nothing
        fetched);
      - `tests/test_fhir_client.py::test_a_malformed_cds_request_is_a_client_error`;
      - `tests/test_fhir.py::test_cds_requires_prefetch` (its `match="prefetch required"`
        still matches the subclass);
      - `tests/test_fhir.py::test_a_base64_attachment_that_is_not_base64_maps_to_nothing`;
      - the 23a golden.
    - Extended: `tests/test_record_hygiene.py`.
  - **Safe alone because:**
    - Decisions on readable input are byte-identical (the 23a golden).
    - ABSTAIN only lowers automation.
    - The record change is additive.
    - The one status change is 422 to 412, when there is neither prefetch nor fhirServer.

- [ ] **23e** A vendor-shaped synthetic profile, and a signed CDS client over HTTP in the sandbox (requirement 10). This is the first end-to-end result.
  - **What it builds:**
    - **`src/mendari/sandbox/ehr_profile.py`**, seeded and deterministic. It builds patients
      for the trial, prior-auth and referral packs, shaped after Epic's and Oracle Health's
      public FHIR documentation, never their data:
      - DocumentReferences linked to Binary: text/plain, text/html, and one RTF note to test
        "unreadable";
      - Conditions with several codings: a fictional site system first, ICD-10-CM second;
      - MedicationRequest with `medicationReference` to a Medication (RxNorm) and a dosage
        `boundsPeriod`;
      - `Coverage.payor` as a reference to an Organization holding a site identifier;
      - ServiceRequests under a site referral category;
      - no SNOMED CT anywhere.
    - **Seeding:** `fhir_workflow.seed_bundle` (`:57-66`) also seeds the trial and prior-auth
      packs, and the profile.
    - **`src/mendari/sandbox/cds_client.py`**, run as `mendari sandbox cds-client`:
      - it makes an EC P-384 key in memory at start and serves its JWKS;
      - it prints its registry entry;
      - it signs every call with `jku` set;
      - it calls discovery and each standard hook (patient-view and order-sign) over HTTP;
      - it exits non-zero on any answer other than 200, or on a failed hook.
    - **Compose and CI:**
      - a `cds-client` service in compose; `engine-signin` registers it through
        `MENDARI_CDS_CLIENTS`, with its JWKS origin on that engine's allow-list;
      - a `sandbox-e2e` step runs it against `engine` (with the flag) and `engine-signin`;
      - `scripts/sandbox_fhir_sample.py` writes the profile's bundles, so `fhir-conformance`
        validates them against US Core 6.1.0.
    - **Scorecard:** the "data shapes" and "conformance evidence" rows, with their scope.
  - **Files:** `src/mendari/sandbox/{ehr_profile.py (new), cds_client.py (new), fhir_workflow.py}`,
    `src/mendari/cli.py`, `docker-compose.yml`, `.github/workflows/ci.yml` (`sandbox-e2e`),
    `scripts/sandbox_fhir_sample.py`, `connecting-an-ehr.md`, the scorecard.
  - **Tests:**
    - `tests/test_ehr_profile.py`:
      - `::test_every_reference_pack_with_a_standard_hook_decides_on_the_vendor_shaped_patients_without_a_failed_hook`
        (the prompt's test). It runs in process through a MockTransport FHIR server built from
        the profile. Every hook answers 200 and every decision is recorded.
      - `::test_what_each_pack_cannot_read_yet_is_pinned`: per pack, its abstentions and missed
        fields, each with its reason. 23h, 23i and 23k update it.
      - `::test_the_profile_has_linked_notes_several_codings_and_medication_requests`.
      - `::test_the_profile_carries_no_snomed_ct`, through `scripts/check_snomed.py`.
    - `tests/test_cds_client_sandbox.py`:
      `::test_the_sandbox_client_signs_what_the_engine_verifies`,
      `::test_it_keeps_its_key_in_memory_only`.
    - The `sandbox-e2e` step itself.
  - **Safe alone because:**
    - It touches the sandbox and CI only, with synthetic data and keys made at runtime.
    - No pack changes.
    - The pinned table is a regression fixture, not a figure: no rate is computed or shown from
      it.

- [ ] **23f** Cards and card feedback as CDS Hooks 2.0 and Da Vinci CRD expect (requirement 5, engine side).
  - **What it builds:**
    - **`cds_hooks.card`** (`:197-246`):
      - `selectionBehavior: "at-most-one"` whenever there are suggestions;
      - the card's `uuid` is the decision id, and the suggestion's uuid is
        `uuid5(UUID(decision id), "suggestion")`;
      - `overrideReasons` come from the new CodeSystem, each Coding with a display.
    - **Summaries:**
      - they come from a new card-only `templates.fill_display` that uses `Question.name_of`
        (`spec.py:424`), and the default verb table uses display names too;
      - `templates.fill` is untouched, so Task text and performer codes do not change;
      - every expansion of every summary is checked to fit 140 characters at pack load, through
        `templates.expansions`.
    - **Detail:** the status label, NOTICE and one plain sentence. The confidence percentage
      leaves the card in every configuration (`cds_hooks.py:222-225`).
    - **When `MENDARI_PUBLIC_URL` is set:**
      - `links` gains an absolute link to `<public>/#audit/<id>`, and the engine trace moves
        behind it;
      - `source.url` is set.

      Without the public URL, the reasons stay in the detail.
    - **The launch link:** a `smart` link to `<public>/smart/launch?pack=<id>`, added only when
      SMART is on and the request's `fhirServer` is a `MENDARI_SMART_EHRS` base of the same
      org.
    - **Discovery** (`:46-73`) gains `extension.davinci-crd.version` and
      `davinci-crd.configuration-options` (coverage-info) for packs with
      `coverage_information`. A request's `extension.davinci-crd.configuration` with
      `coverage-info: false` drops the system actions.
    - **The audit view** (`dashboard/audit.js`, at `#audit/<id>`, the cards' link target) gains
      a data-coverage panel: counts by field and reason, never "k of n". Styles use tokens
      only.
    - **The feedback route,** `POST /cds-services/{service_id}/feedback`:
      - A `_RULES` entry `("POST", r"^/cds-services/[^/]+/feedback$", "decide")` sits before
        the catch-all, with DECIDE in `_ROUTE_SCOPES`.
      - The body is a closed pydantic model:
        - 1 to 50 items;
        - `outcome` is accepted or overridden;
        - `acceptedSuggestions` is required when accepted, and must be that card's suggestion
          ids;
        - `overrideReason.reason` must be one of the Codings the card offered;
        - `outcomeTimestamp`;
        - `userComment` is dropped.
      - Each card must be a decision of that service's pack in the caller's org. With sign-in
        on, it must also have been asked for by the same caller: the chained `requested_by`
        (`AuditStore.record`). Otherwise every item gets one 404.
      - It is all or nothing.
    - **The chain:**
      - `DECISION_KINDS` (`audit.py:134`) gains `card.feedback`;
      - `AuditStore.record_card_feedback` calls `record_event(id, "card.feedback", payload)`
        with the literal kind, as `tests/test_audit_fhir.py::recorded_kinds` requires;
      - the payload holds the service, outcome, accepted suggestion ids, reason
        `{system, code}` and timestamp, and no note;
      - `audit_fhir._EVENTS` and `terminology._audit_events` gain the kind;
      - the new CodeSystem `https://mendari.co/fhir/CodeSystem/card-override-reasons` is
        defined in `terminology.py`.
    - **Limits:** the limiter's decide bucket (`Limiter.classify`, `limits.py:339`). The route
      is not in `_DECIDES`, so it is not daily-capped.
    - **Registers in:**
      - the calls of the `cds-hooks` preset (`presets.py:117-125`);
      - the inventory at `tests/test_shipped.py:172`, as `("card.feedback",)`.
    - **Docs:** `docs/guides/api.md`, `connecting-an-ehr.md`, threat-model rows R5 (feedback
      naming another organization's, pack's or caller's decision) and I18 (the decision id as
      the card's uuid, already shown in its extension), the scorecard's "cards and workflow"
      rows.
  - **Files:**
    - Code: `src/mendari/standards/{cds_hooks.py, audit_fhir.py, terminology.py}`,
      `src/mendari/core/{templates.py, spec.py (the load check), audit.py}`,
      `src/mendari/server/{app.py, auth.py, scopes.py, presets.py}`,
      `src/mendari/dashboard/audit.js`.
    - Docs: as listed above.
  - **Tests:**
    - `tests/test_cds_cards.py`:
      - `test_a_card_with_suggestions_says_its_selection_behavior`;
      - `test_the_card_uuid_is_its_decision_id_and_suggestion_ids_are_stable`;
      - `test_card_summaries_use_display_names_and_task_text_is_unchanged`;
      - `test_every_summary_expansion_fits_140_characters`;
      - `test_no_card_shows_a_bare_percentage`;
      - `test_the_engine_trace_is_behind_the_audit_link_only_when_one_can_be_made`;
      - `test_a_card_links_to_the_launch_view_only_for_a_registered_ehr_of_the_same_organization`;
      - `test_discovery_declares_the_crd_version_and_configuration_options`;
      - `test_coverage_info_false_sends_no_coverage_information`;
      - `test_every_card_matches_the_cds_hooks_2_0_card_shape`.
    - `tests/test_cds_feedback.py`:
      - `test_feedback_for_an_accepted_and_an_overridden_card_is_recorded_and_chained`;
      - `test_feedback_naming_another_organizations_packs_or_callers_decision_answers_as_unknown`;
      - `test_an_override_reason_or_suggestion_the_card_did_not_offer_is_refused_and_nothing_is_written`;
      - `test_the_comment_is_not_kept`;
      - `test_feedback_is_rate_limited_and_not_daily_capped`;
      - `test_audit_verify_passes_after_feedback` (SQLite and Postgres);
      - `test_the_override_reasons_are_published_with_displays`.
    - The CodeSystem runs through the `fhir-conformance` validator.
    - A browser case in `tests/browser/test_dashboard.py` for the panel, with axe and
      `BARE_PERCENT`.
    - Updated inventories:
      - `tests/test_auth.py::test_every_route_has_the_permission_it_should` and
        `::test_every_route_principal_role_and_scope_gets_what_it_should`;
      - `tests/test_origin.py::test_every_route_and_kind_of_request_gets_what_it_should`;
      - `tests/test_shipped.py::test_every_state_changing_route_is_in_the_inventory`;
      - `tests/test_two_organizations.py::test_every_route_called_as_two_organizations_shows_neither_the_others_data`;
      - `tests/test_evalset_leaks.py::test_no_answer_log_export_or_command_shows_a_hidden_set`,
        whose `world.called == every` (`:1501`) needs the new call;
      - `tests/test_limits.py::test_costly_routes_have_their_class_reads_none_and_one_call_of_each_passes`
        (`EXPECTED_CLASSES`);
      - `tests/test_key_presets.py`.
    - Updated card strings, only where a display name differs from the raw label:
      `tests/test_fhir_lists.py::test_the_inbox_card_wording_is_unchanged` (and the trial
      summary at `:164` if it changes), `tests/test_order_sign.py` (`:109`) and
      `tests/test_templates.py` (`:79`).
    - Unchanged: `tests/test_notice.py::test_cds_hooks_carry_it` and
      `tests/test_pack_status.py::test_cds_hooks_say_it_in_discovery_and_on_every_card`.
  - **Safe alone because:**
    - Decisions and Tasks do not change. Only card JSON and discovery do, and links appear only
      when they are configured.
    - The route sits behind a `cds` principal or a DECIDE key, and chains codes and ids only.

- [ ] **23g** Mapping syntax 0.3 (engine and spec; no shipped pack adopts it).
  - **What it builds:**
    - `docs/spec/decision-pack-0.3.md`: the FHIR mapping section is new, the rest is as 0.2. It
      gets a nav entry, and `decision-pack-0.2.md` gets a pointer to it.
    - `core/spec.py`: `spec_version` (`:662`) gains `"0.3"`. A 0.1 or 0.2 pack that uses 0.3
      mapping keys is refused at load, as `:763` refuses 0.2 features in a 0.1 pack.
    - `standards/fhir.py`:
      - `_SEGMENT` (`:48`) gains the predicate `coding[system=<declared uri>]`.
      - `FieldMapping.paths:`, where the first path that finds something wins.
      - `ListMapping.resources:`, several resource types into one target, each with its own
        fields.
      - `follow:`, one reference hop, resolved only inside the indexed record (contained
        resources and Bundle entries), with no fetch.
      - The `attachment_text` transform. It reads inline `data` or content that 23h attaches.
        text/plain is read as is and text/html through stdlib `html.parser`, with scripts and
        styles dropped. Any other type counts `unsupported_type`.
      - The `medication_period` derivation (conflict 39).
      - `code_sets: {name: {system, codes}}`, and a `code_set:` selector that takes a site
        translation map as an argument. Nothing passes one until 23i.
      - The adapter tells `data_coverage.measure` what each field looked for.
    - For a pack the deployment does not own, the new keys are bounded and checked at load: at
      most 4 paths per field, one `follow` hop, and at most 200 codes per code set.
    - Docs: `docs/guides/authoring-a-pack.md`, and the threat model's "Pack content" `inputs.fhir`
      row (`threat-model.md:117`), which names the new keys and their bounds.
  - **Files:** `src/mendari/core/spec.py`, `src/mendari/standards/fhir.py`, the spec documents,
    `mkdocs.yml`, `authoring-a-pack.md`, the threat model, `tests/test_fhir_mapping_03.py`,
    `tests/test_spec_03.py`.
  - **Tests:**
    - `tests/test_fhir_mapping_03.py`:
      - `test_a_coding_is_chosen_by_its_system_whatever_its_position`;
      - `test_a_fallback_path_is_read_only_when_the_first_finds_nothing`;
      - `test_one_list_reads_two_resource_types_by_their_own_fields`;
      - `test_a_medication_requests_start_and_stop_follow_the_stated_derivation` (a table of
        cases);
      - `test_a_reference_is_followed_inside_the_record_only`;
      - `test_an_html_note_is_read_as_text_and_another_type_is_unreadable`;
      - `test_a_code_set_selector_matches_only_its_declared_codes`;
      - `test_an_outside_packs_new_keys_are_bounded_at_load`.
    - `tests/test_spec_03.py`: `::test_0_3_mapping_keys_in_a_0_2_pack_are_refused_at_load`,
      `::test_every_0_1_and_0_2_pack_still_loads_and_maps_identically` (the 23a golden).
    - Unchanged: `tests/test_trial_pack.py::test_through_fhir_a_condition_is_read_by_its_first_coding_only`.
  - **Safe alone because:** no shipped pack declares 0.3, 0.2 mappings read as before (the
    golden), and the code is pure.

- [ ] **23h** Linked record data: notes by Binary link, and the payer and medication by reference, all read under the EHR's FHIR server.
  - **What it builds:**
    - `src/mendari/standards/linked.py`. It runs in `cds_hooks.invoke` and
      `smart_routes.decide_for`, before `decide_fhir`. It handles the 0.3 `attachment_text`
      fields and `follow` hops that the prefetch does not cover.
    - **Reads:**
      - they go through `FhirClient.read`, whose `_resolve` (`fhir_client.py:239-248`) keeps
        paths under the base;
      - an absolute URL anywhere else counts `off_server` and is never fetched;
      - Binary is read only as FHIR JSON (`Accept: application/fhir+json`), since
        `GuardedHttp` refuses anything else.
    - **Caps:** at most 20 linked reads per decision and 1 MiB decoded each. Going over counts
      `cap` and abstains.
    - **References:**
      - `Coverage.payor[0].reference` resolves to its Organization: contained, in the prefetch,
        or by one read;
      - a `medicationReference` is read once per id.
    - Failures become 23d reason codes.
    - `fhir/decide` and `$decide` never fetch, so a link there counts `not_fetched`.
    - Docs: the E1 threat-model row is updated, and a new D14 covers linked-read volume.
      `connecting-an-ehr.md` and the spec 0.3 document are updated.
  - **Files:** `src/mendari/standards/{linked.py (new), cds_hooks.py}`,
    `src/mendari/server/smart_routes.py`, the docs above, `tests/test_fhir_linked.py`,
    `tests/test_ehr_profile.py` (its table).
  - **Tests** (`tests/test_fhir_linked.py`):
    - `test_a_linked_note_is_read_through_the_allow_listed_client`;
    - `test_an_attachment_off_the_fhir_server_is_never_fetched_and_counts_unreadable`;
    - `test_linked_reads_are_capped_per_decision_and_in_size`;
    - `test_html_is_read_as_text_and_rtf_abstains`;
    - `test_the_payer_is_read_by_its_coverage_reference`;
    - `test_a_medication_reference_is_read_once`;
    - `test_without_a_server_a_link_is_not_fetched_and_says_so`;
    - `test_no_note_text_reaches_the_coverage_record_reasons_or_the_chain`.
  - **Safe alone because:** no shipped pack uses 0.3 until 23k, and every read goes through the
    allow-list, stays under the base and is capped.

- [ ] **23i** A site code map per organization, versioned and hashed into each decision, and never automated.
  - **What it builds:**
    - `src/mendari/core/site_codes.py` reads `MENDARI_SITE_CODES`, a path or inline JSON of the
      form `{org: {version, sets: {<code set>: [{system, code, as}]}}}`.
      - It is checked at startup against the code sets the loaded packs declare. A translation
        must land on a code its set declares.
      - It has a canonical form, and its sha256 is taken over that form
        (`mendari.site-codes/1`).
    - `Decision` passes the caller organization's map to the 0.3 `code_set` selector. Each FHIR
      decision records `site_codes: {version, sha256} | null` beside `data_coverage`.
    - `core/policy.py` gains a cap, `site_code`: a decision whose input used a translation goes
      from AUTO to HUMAN_REVIEW.
    - `./scripts/build-reference-pack.sh` is rerun because policy code changed. No artifact diff
      is expected, and the PR says so.
    - `MENDARI_SITE_CODES` joins `LIVE_SETTINGS`, with a §8 row in `docs/deploying-with-phi.md`.
    - Docs: `connecting-an-ehr.md` (examples use fictional codes only), threat-model row T9 (a
      wrong or swapped site map), the scorecard.
  - **Files:** `src/mendari/core/{site_codes.py (new), decision.py, policy.py}`,
    `src/mendari/standards/fhir.py`, `src/mendari/server/app.py`, `tests/conftest.py`, the
    docs above.
  - **Tests** (`tests/test_site_codes.py`):
    - `test_unset_means_every_decision_as_before`;
    - `test_the_map_is_checked_at_startup`;
    - `test_a_site_code_is_translated_only_into_a_code_its_set_declares`;
    - `test_each_decision_records_the_maps_version_and_digest_for_its_organization`;
    - `test_a_decision_made_through_a_site_code_is_never_automated`;
    - `test_another_organizations_map_is_never_applied`;
    - `test_a_changed_map_is_told_apart_by_digest`;
    - `test_old_records_still_verify` (SQLite and Postgres).
    - Also `tests/test_deploy.py::test_every_setting_the_templates_use_is_documented`.
  - **Safe alone because:** with the map unset nothing changes, no shipped pack declares a code
    set until 23k, and the cap only lowers automation.

- [ ] **23j** Pack cards: the inbox's card template, and referral cards only when action is needed. **The owner confirms the `show` list.**
  - **What it builds:**
    - `patient-inbox-router` gains a `cds_hooks.card` block:
      - an indicator per team;
      - a `none` summary;
      - a suggestion label.

      Team summaries keep the default outcome wording, with display names (conflict 45). The
      hook stays `mendari-message-received`, off Inferno's list.
    - `referral-router` gains `show: [return_for_info, urgent_review]`.
    - Both packs get a version bump, both changelogs are updated, and
      `./scripts/build-reference-pack.sh` is rerun. Data, models, `evals/benchmark.json` and
      `DECISION_CARD.md` are committed together.
    - The registry's reference index is updated.
  - **Files:** `packs/{patient-inbox-router,referral-router}/pack.yaml` and their regenerated
    artifacts.
  - **Tests:**
    - `tests/test_inbox_pack.py::test_the_inbox_card_has_its_own_template`.
    - `tests/test_referral_pack.py::test_only_referrals_that_need_action_show_a_card`: the
      others are decided and audited, but not shown.
    - `tests/test_registry_engine.py::test_the_reference_index_is_each_bundled_packs_id_version_and_digest`.
    - The existing inbox CDS fixtures still pass, and the 23a golden is unchanged.
    - The PR states the effect on the critical-label figures: none, because only cards change.
  - **Safe alone because:** no decision changes, only which cards show and what they say.

- [ ] **23k** Pack mappings for real EHR data, on spec 0.3. **Needs the owner's go-ahead: it changes FHIR-path decisions in three packs.**
  - **What it builds:**
    - **`clinical-trial-prescreener`:**
      - Conditions are read by system: ICD-10-CM, then the FC system. This fixes the
        first-coding miss, and the owner accepted that this selector would fix it on
        2026-10-08 (`tasks/todo.md:316-323`).
      - `medications` come from MedicationRequest and MedicationStatement, with the
        derivation, and a `MedicationRequest?patient=` prefetch is added.
      - Notes are read through `attachment_text`.
    - **`prior-auth-preflight`:**
      - The payer is read through the `Coverage.payor` reference to
        `Organization.identifier[system=…]`, then the site map, falling back to the literal
        identifier.
      - The order code is read by system.
      - Notes are read through `attachment_text`.
    - **`referral-router`:**
      - `focus_where` goes through code set `referral-category`, which declares FR101.
      - The codes `logic/checklist.yaml` looks for are declared as code sets in the mapping, so
        a site's own codes translate into them through the 23i map (A12.3e). The logic files do
        not change.
    - All three packs:
      - get version bumps and are rebuilt;
      - each card's known failure modes state the medication derivation;
      - the 23a golden diff is explained in the PR;
      - the 23e table is updated, and the 23l golden is refreshed if 23l has landed.
  - **Files:** `packs/{clinical-trial-prescreener,prior-auth-preflight,referral-router}/{pack.yaml,fhir/mapping.yaml}`,
    the regenerated artifacts, the changelogs, `tests/fixtures/fhir/decisions-golden.json`.
  - **Tests:**
    - `tests/test_trial_pack.py::test_through_fhir_a_condition_is_read_by_its_icd10_coding_wherever_it_is`
      replaces `::test_through_fhir_a_condition_is_read_by_its_first_coding_only`.
    - `tests/test_trial_pack.py::test_a_medication_request_is_read_with_its_stated_start_and_stop`.
    - `tests/test_prior_auth_pack.py::test_the_payer_is_read_by_coverage_reference`.
    - `tests/test_referral_pack.py::test_a_referral_under_a_site_code_is_decided_through_the_site_code_map`.
      The existing `::test_a_referral_under_a_site_code_is_decided_once_the_site_lists_it`
      stays true and is kept.
    - `tests/test_ehr_profile.py::test_what_each_pack_cannot_read_yet_is_pinned` now pins only
      the RTF note.
    - The PR states the effect on the critical-label figures: none, because benchmarks run on
      the JSON datasets, which do not change.
  - **Safe alone because:**
    - JSON-path decisions do not change.
    - FHIR-path decisions change only where the 0.2 mapping misread the data.
    - Site translations are capped at HUMAN_REVIEW, and every new failure abstains.

- [ ] **23l** Conformance outside the unit tests (requirement 9): Inferno's CRD kit as the CDS client, the SMART App Launch and Bulk Data client suites, the HL7 validator with a terminology server on a schedule, and the vendor scope check.
  - **What it builds:**
    - `sandbox/inferno/Dockerfile`: `FROM ruby@sha256:…`, with `davinci-crd-test-kit` at the
      v0.14.2 commit SHA and its `Gemfile.lock`. The kit publishes no image.
    - `sandbox/inferno/docker-compose.yml`: inferno, its worker, redis,
      `inferno-resource-validator`, `fhirpath-service` and nginx, each pinned by digest.
    - **The CRD session.** `sandbox/inferno/crd_server_v221.yaml` (an `inferno execute_script`
      session) and its golden, `crd_server_v221_expected.json`:
      - The golden lists every test id with pass, fail or skip. Each expected failure or skip
        has its reason: 1.01 (TLS over plain HTTP), the CRD standard prefetch warnings, and
        whatever 23f has not yet flipped.
      - Any difference fails the job, an unexpected pass included.
      - It runs against `engine-signin`, whose `MENDARI_CDS_CLIENTS` registers Inferno's iss
        (`<inferno base>/custom/crd_server_v221`) and its jku (`<iss>/jwks.json`).
      - `inferno`, a single-label host, goes on `MENDARI_FHIR_ALLOWED_SERVERS`, and
        `MENDARI_PUBLIC_URL` is the URL Inferno calls.
      - The Mock EHR Data comes from 23e's prior-auth patients.
      - The ignore list is trial, referral and inbox.
    - **The other kits:**
      - the SMART App Launch kit at v1.0.3, suite `smart_client_stu2_2`, with Inferno as the
        EHR against `/smart/launch`;
      - the Bulk Data kit at v0.13.1, suite `v2.0.0_client`, with Inferno serving `$export` to
        Mendari's Bulk client;
      - each has its own golden; if the SMART client suite cannot run headless, its golden says
        so and it is run by hand;
      - kit versions are re-checked and pinned by commit SHA at build time.
    - **`.github/workflows/conformance.yml`:**
      - triggers: `workflow_dispatch`, weekly, and `pull_request` on the CDS paths
        (`standards/cds_hooks.py`, `server/cds_clients.py`, `sandbox/inferno/**`);
      - it is not a required check;
      - it holds the Inferno jobs and a scheduled `validator-tx` job;
      - `validator-tx` runs `scripts/fhir_validate.py --tx https://tx.fhir.org/r4` over the
        inputs `fhir-conformance` validates; the default stays `-tx n/a` (`:44`);
      - the offline `fhir-conformance` job in `ci.yml` stays the required gate.
    - **The scope check.** `mendari smart check` (`server/smart_check.py:108-125`) compares the
      scopes Mendari asks for (`MENDARI_SMART_SCOPES`, else `DEFAULT_SCOPES`, `smart.py:50`)
      with the EHR's `scopes_supported`:
      - a scope the EHR does not publish is a failure line;
      - an EHR that publishes no scopes gives `unknown`, never a pass;
      - `sandbox-e2e` runs the check against smart-launcher-2.
  - **Files:** `sandbox/inferno/*` (new), `.github/workflows/conformance.yml` (new),
    `scripts/fhir_validate.py`, `src/mendari/server/smart_check.py`, `.github/workflows/ci.yml`
    (`sandbox-e2e`), `docs/deploying-with-phi.md` §9, `connecting-an-ehr.md`, the scorecard.
  - **Tests:**
    - `tests/test_inferno_setup.py`: `::test_the_overlay_and_dockerfile_are_pinned`,
      `::test_every_expected_failure_or_skip_has_a_reason`,
      `::test_the_job_registers_infernos_issuer_and_holds_no_secret`.
    - `tests/test_supply_chain.py`: `COMPOSE_FILES` (`:22`) gains the overlay, and
      `test_every_dockerfile_base_is_pinned_by_digest` (`:281`) also reads
      `sandbox/inferno/Dockerfile`.
    - The parametrised `tests/test_workflow_safety.py` cases over `conformance.yml`, plus
      `::test_terminology_validation_runs_on_a_schedule_and_never_on_a_pull_request`.
    - `tests/test_fhir_validate.py::test_offline_is_the_default_and_tx_is_opt_in` (new file).
    - `tests/test_smart_check.py`: `::test_a_scope_the_ehr_does_not_publish_fails`,
      `::test_unpublished_scopes_are_unknown_not_passed`.
  - **Safe alone because:** it adds CI, sandbox files and a read-only check. The new workflow
    is not required, and the required gate does not change.

- [ ] **23m** Close-out.
  - **What it builds:**
    - **`launch/gates.yaml` `a12-ehr-pilot`.** Its pending probe is replaced by these tests:
      - `tests/test_cds_clients.py::{test_a_valid_token_is_accepted, test_a_wrong_issuer_is_refused, test_a_wrong_audience_is_refused, test_an_expired_token_is_refused, test_a_replayed_jti_is_refused_and_a_jti_is_per_client, test_an_unregistered_jku_is_refused}`
      - `tests/test_cds_auth.py::{test_a_valid_token_reads_discovery_and_calls_the_hook, test_a_cds_client_reaches_nothing_but_cds_services}`
      - `tests/test_cds_open_mode.py::test_open_mode_answers_cds_hooks_on_this_machine_only`
      - `tests/test_cds_cors.py::test_a_registered_origin_gets_cors_on_cds_services_only`
      - `tests/test_cds_unreadable.py::test_a_fetch_failure_gives_abstain_with_a_reason_and_a_recorded_decision`
      - `tests/test_data_coverage.py::test_every_fhir_decision_carries_its_data_coverage`
      - `tests/test_ehr_profile.py::test_every_reference_pack_with_a_standard_hook_decides_on_the_vendor_shaped_patients_without_a_failed_hook`
      - `tests/test_cds_cards.py::test_a_card_with_suggestions_says_its_selection_behavior`
      - `tests/test_cds_feedback.py::test_feedback_for_an_accepted_and_an_overridden_card_is_recorded_and_chained`
      - `tests/test_fhir_linked.py::test_a_linked_note_is_read_through_the_allow_listed_client`
      - `tests/test_site_codes.py::test_each_decision_records_the_maps_version_and_digest_for_its_organization`
      - `tests/test_inferno_setup.py::test_every_expected_failure_or_skip_has_a_reason`
      - `tests/test_ehr_pilot_e2e.py::test_a_signed_hook_its_card_and_its_feedback_verify_end_to_end`

      It also gets `file: sandbox/inferno/crd_server_v221_expected.json` and one owner line:
      `pending: "owner: the Epic sandbox run from notes/epic-sandbox-checklist.md (every
      standard-hook pack decides on Epic's sandbox patients, A12.acc-3) and mendari smart check
      against Epic; then a build PR removes this line"`. This is the pattern the monitor gate
      uses for LP-21n.
    - The URL of the first green `conformance.yml` run on main is recorded in the todo build
      log. That green run is the acceptance "the CRD client job passes".
    - **The threat model's residual risks:** trust in the EHR's key and its rotation, a
      compromised jku host, the decision id disclosed to the EHR, a misconfigured site map, and
      frequent abstention on RTF-heavy records.
    - **Final documents:** ADR-0014 is marked Accepted. The scorecard's "after" column is graded
      at the close-out commit, with its scope.
    - **Docs:** `docs/deploying-with-phi.md` §2, §8 and §9, `connecting-an-ehr.md`, `api.md`, the
      `NOT_A_DATA_SERVER` text in `standards/capability.py`, `deploy/vercel/README.md`, the
      `/docs` landing, `README.md`, `CHANGELOG.md` and the todo build log.
  - **Tests:** `tests/test_ehr_pilot_e2e.py::test_a_signed_hook_its_card_and_its_feedback_verify_end_to_end`,
    followed by `mendari audit verify` on SQLite and Postgres.
  - **Safe alone because:** it adds docs, gate probes and one test.

## Conflicts written before building, and the default each PR follows

1. **(23a) The scorecard.** The "2.5 of 5" and its rubric exist nowhere in the repository or its
   history (A12.goal). Default:
   - rubric v1, graded at main;
   - the overall grade is the lowest dimension;
   - every grade carries the scope line from the Summary;
   - the old grade is not reproduced;
   - the owner approves the rubric, or supplies the original.
2. **(23b) The bearer cap.** `_BEARER` caps credentials at 512 characters (`auth.py:555`), and
   `_bearer` is not given the path. Default:
   - a JWT-shaped credential of at most 8 KiB (`MAX_ASSERTION_BYTES`) is recognised on
     `^/cds-services(/|$)` only;
   - everywhere else it stays `malformed` or `unknown_prefix`;
   - `ohk_` and `mat_` do not change.
3. **(23b) The prompt says "no unverified JWT in any mode", but open mode never reads
   Authorization.** `Auth.check` returns at `auth.py:1062` before `authenticate`, and
   `principal_of` falls back to `OPEN` (`app.py:749`). Default:
   - the CDS branch sits above that shortcut, and verifies a JWT-shaped header in both modes;
   - without a registry, such a header gets 401;
   - with sign-in off, every registered org must be `default`.

   As a result, the public CDS Hooks Sandbox, which sends its own JWT, has to be registered even
   for a local demo.
4. **(23b) `aud`.** Default: `MENDARI_PUBLIC_URL` plus the request path, compared exactly and
   never with the Host header. A registry without `MENDARI_PUBLIC_URL` stops startup. Inferno
   sends the service URL for hooks and `<base>/cds-services` for discovery, which this
   matches. Epic's form is checked in the sandbox. No `audience: base` option is built until
   Epic shows it needs one.
5. **(23b) `iss` against the FHIR base.** B38 says Epic's iss is the FHIR base; Inferno's is
   `<inferno>/custom/crd_server_v221`. Default: iss, keys and `fhir_servers` are registered
   separately, and an optional per-client `iss_is_fhir_server` requires `iss == fhirServer`.
6. **(23b) `sub` and `tenant`.** B38 lists `sub`; CDS Hooks 2.0 does not define it, and
   Inferno's JWT has none (`jwt_helper.rb`: `{iss, aud, exp, iat, jti}`). Default: `sub` is
   optional, and checked only when the registry pins it. A `tenant` claim is refused unless it
   equals the registered value, since Community Connect is not supported.
7. **(23b) `iat`.** The prompt requires it, and `verify_assertion` does not check it. Default:
   `iat` is required, may not be in the future beyond the 30 s skew, and `exp − iat ≤ 330`.
   Inferno's tokens live 300 s.
8. **(23b) The header.** `backend_services._HEADER_FIELDS` refuses `jku` (`:83`). Default:
   - the CDS verifier has its own set, {alg, kid, typ, jku}, with `typ` required to be `JWT` and
     `kid` required;
   - `check_jwks` and `ALGORITHMS` are imported from `backend_services`, which does not change.
9. **(23b) The JWKS fetch and SSRF.** Default:
   - `jku` must equal the registered `jwks_uri` exactly, and its origin must be on
     `MENDARI_FHIR_ALLOWED_SERVERS`, so one allow-list governs every outbound EHR call (E1);
   - keys are fetched through `GuardedHttp` and live 3600 s;
   - an unknown kid refetches at most once per client per 60 s;
   - an answer holding a private member caches nothing.
10. **(23b) Where the registry lives** (owner decision 1). Default: `MENDARI_CDS_CLIENTS`, a JSON
    list checked at startup like `MENDARI_SMART_EHRS`. Changing it means a redeploy.
11. **(23b) "The same credential reads discovery" against jti replay.** A jti is good for one
    request, so one token cannot serve both discovery and a hook. Default: "the same
    credential" means the same registered client and keys.

    A DECIDE-scoped key already reads discovery (`scopes.py:188`), so audit row A12.1c is
    stale for keys.
12. **(23b) Confining the principal.** The `decide` permission also reaches
    `/api/packs/*/decide` and `$decide`. Default:
    - kind `cds` holds {discover, decide} and is confined to `CDS_PATH` in `Auth.check`, as
      `LAUNCH_PATH` confines `ehr`;
    - it is exempt from the notice gate, which covers `user` only (`app.py:2798`);
    - the daily cap applies (`kind != "open"`, `app.py:2463`), and the limiter buckets it by
      `cds:<id>`;
    - preset keys keep working.
13. **(23b) Replay claims at CDS volume.** `smart_claim` has no expiry purge
    (`audit.py:2792-2810`); `smart_put` has one (`:2665-2672`). Default: add the same bounded
    purge. Backend Services behaves as before.
14. **(23b, 23d) A `fhirServer` off the allow-list or not registered.** Default:
    - for a `cds` principal, 403 before charging or fetching;
    - otherwise 422, as today, with nothing fetched or recorded;
    - this is configuration, not data, so it does not abstain;
    - `tests/test_fhir_client.py::test_a_server_off_the_list_is_refused` does not change.

    This departs from Plan A, which abstained here.
15. **(23c) "A local host."** `is_local_host` and `sessions.local` read the Host header, which a
    caller can spoof. TestClient connects as `"testclient"` with Host `testserver`. Default:
    - local means a loopback `limits.client_ip`, a local Host and no forwarding header;
    - otherwise `MENDARI_CDS_SANDBOX=1` must be set, and it is refused on `VERCEL` or with
      sign-in on;
    - a verified token does not lift the rule in open mode, so Inferno runs against sign-in on;
    - the suite sets the flag in an autouse fixture, and `tests/test_cds_open_mode.py` unsets
      it to prove the default.
16. **(23c) CORS against CORP `same-origin`, COEP and `OriginCheck`.** `OriginCheck` lets bearer
    requests through only with sign-in on (`origin.py:73-74`), and an OPTIONS preflight is not
    in `SAFE_METHODS` (`:54`). Default:
    - CORS on `CDS_PATH` only, for exact registered origins, without credentials;
    - the preflight is answered before the guard;
    - a cross-site Origin must belong to the verified client;
    - CORP stays as it is, because it governs no-cors loads only; a browser test proves a CORS
      read works.
17. **(23d) CDS Hooks 2.0's 412 against "the hook still answers".** Default:
    - neither prefetch nor fhirServer gives 412, as CDS Hooks says;
    - a failed read from an allowed server gives 200 with a recorded ABSTAIN, as LP-23 says and
      as Inferno CRD v2.2.1's Technical Issues group expects;
    - `test_without_a_fhir_server_prefetch_is_still_required` changes from 422 to 412.
18. **(23d) A null prefetch value.** Today it triggers a fetch (`cds_hooks.py:105`). CDS Hooks
    2.0 uses null for "no data". Default:
    - null means no data, and is never fetched;
    - a key left out is fetched;
    - an OperationOutcome value is unreadable;
    - a required resource the client says it has no data for is still a 422 from `index()`
      (`fhir.py:159-162`), as a request fault.
19. **(23d) Which failures abstain.** Default: any declared prefetch key that could not be read,
    with fixed reason codes and never a URL, query or server text.
    - Request faults stay 4xx: the wrong hook, too many orders, a malformed context or field,
      and neither prefetch nor fhirServer.
    - Structure errors in what the client sent stay 400.
    - Structure errors in resources Mendari fetched itself mark that key `not_r4`. No decision
      is ever made on them, so the R4 gate is not weakened.
20. **(23d) How to abstain.** `decide()` has only BLOCK for rejected input. Default:
    - a new `unreadable=` argument;
    - rejected input still gives BLOCK;
    - otherwise ABSTAIN, before any provider runs and before `index()` can raise for a resource
      whose read failed.

    Plan B's "short-circuit before the schema" would have turned some BLOCKs into ABSTAINs.
21. **(23d) `base64_text` on 0.2 packs.** A url-only note, or data that does not decode, reads
    today as silently "no notes". Default:
    - it counts `not_fetched` or `undecodable`, and abstains for every pack, which only lowers
      automation;
    - `_decode` and `test_a_base64_attachment_that_is_not_base64_maps_to_nothing` do not
      change;
    - none of the golden's fixtures change, but decisions on such inputs move from a possible
      AUTO to ABSTAIN, and the CHANGELOG says so.
22. **(23d) "Counts and codes" against patient data.** A patient's codes in a permanent chained
    record are clinical data. Default:
    - only declared system URIs, the pack's own looked-for codes, counts and reason codes;
    - never the patient's codes, values, ids or text;
    - an undeclared system counts as a number only;
    - the name is `data_coverage` (`mendari.data-coverage/1`), because `mendari.coverage/1` is
      LP-11's (`evals/coverage.py:28`), and "coverage" is both a prior-auth prefetch key and a
      CRD term.
23. **(23d) "On every decision."** Mapped fields exist only for FHIR input. Default: every
    FHIR-mapped decision carries the record (CDS, `fhir/decide`, `$decide`, SMART decide, DTR),
    JSON decisions carry null, and old records still verify (Definition of shipped §11).
24. **(23d) SMART decide.** The prompt names hooks. Default: `smart_routes.decide_for` shares the
    per-key read, so a failed prefetch read abstains, which lowers automation only. A failed
    focus read keeps today's error, because there is nothing to decide about.
25. **(23d) `show` would hide an unreadable ABSTAIN.** `recommendation=None` shows as `none`,
    which the trial and prior-auth `show` lists leave out. A silent card would tell the
    clinician nothing was wrong. Default: an ABSTAIN over unreadable data always shows an `info`
    card (owner decision 6). Showing a card never raises automation.
26. **(23d) CRD on a failed read.** The coverage-information extension needs a Coverage
    reference. Inferno's group requires `indeterminate`, `technical` and a reason `text`
    (`technical_issues_group.rb`, `require_reason_text: true`, v0.14.2). Default:
    - the system action is sent only when the Coverage was read, with fixed text;
    - when it was not, there is no system action and the card says so;
    - `CoverageInformation.covered` stays as it is: the engine emits `indeterminate`, and a
      pack cannot declare it.
27. **(23e) The vendor profile's codes.** Default:
    - fictional site systems plus ICD-10-CM, LOINC and RxNorm, and no SNOMED CT
      (`scripts/check_snomed.py`, `tests/test_code_systems.py`);
    - shaped after public vendor documentation, never vendor data;
    - checked against the sandbox once the owner has it.
28. **(23e) "Every reference pack decides on the vendor's sandbox patients."** Default:
    - the synthetic profile stands in for them;
    - Epic's own patients are the owner's acceptance run (A12.acc-3), shown as a pending line in
      the gate;
    - the pinned table of what each pack cannot read is a regression fixture, and no rate is
      computed from it.
29. **(23f) Display names.** `templates.fill` also writes Task text and performer codes
    (`fhir.py:219-269`). Default:
    - a card-only `fill_display` with `name_of`, which falls back to `readable()` and so
      capitalises;
    - every expansion is checked to fit 140 characters at load through `templates.expansions`;
    - Task text does not change;
    - the card-string tests change only where a display name differs from the raw label.
30. **(23f) The card shows a bare `confidence: 73%`** (`cds_hooks.py:222-225`), against
    Definition of shipped §3 and the rule that every figure carries its interval and scope.
    Default: the confidence leaves the card in every configuration. No test pins it.
31. **(23f) "Engine detail behind the audit link."** The audit view needs a Mendari sign-in with
    `read` in that organization. Default:
    - the trace moves behind the link only when `MENDARI_PUBLIC_URL` is set;
    - the card keeps the status, the notice and one plain sentence;
    - clinicians without a Mendari account see no trace (owner decision 11).
32. **(23f) The launch link.** `smart.complete()` drops `appContext`. Default: a `?pack=` launch
    URL, only when it is fully configured. `appContext` waits for Epic's behaviour (owner
    decision 12).
33. **(23f) The card uuid** is random and not stored (`cds_hooks.py:230`). Default:
    - the card uuid is the decision id, already shown in the card's extension;
    - the suggestion uuid is its `uuid5`;
    - no storage is added;
    - the disclosure is recorded in I18.
34. **(23f) The feedback route.** Unlisted routes need `admin`. The existing
    `POST /api/audit/{id}/feedback` is a person's rating, needs `review` and means something
    else. Default:
    - a `decide` rule and a DECIDE scope;
    - the same pack and organization, plus, with sign-in on, the decision's chained
      `requested_by`;
    - one 404 for anything else;
    - all or nothing;
    - the decide bucket, with no daily cap;
    - the new kind `card.feedback`, holding codes and ids only.
35. **(23f) `userComment`** is free text (owner decision 7). Default: dropped, so the chain holds
    codes and ids only.
36. **(23f) CRD card rules.** `source.topic` is required by some CRD profiles, and this was not
    verified here. Default: add it only if the first Inferno run fails without it, and record
    that in the golden.
37. **(23g) Mapping syntax is a spec change.** `FhirMapping` forbids extra keys (`fhir.py:52`).
    Default:
    - a new document, `decision-pack-0.3.md`;
    - 0.1 and 0.2 packs load and map identically;
    - 0.3 keys in an older pack are refused;
    - an older engine cannot load a 0.3 pack, and the spec says so.
38. **(23g) Packs the deployment does not own.** Default: the 0.3 keys are bounded and checked at
    load (paths, one `follow` hop, code-set size), as other mapping content is
    (`threat-model.md:117`).
39. **(23g) The MedicationRequest derivation** is a clinical judgement, and the drafts
    disagreed. `dispenseRequest.validityPeriod` is the dispensing window, not when the patient
    takes the drug. Default:
    - start is `dosageInstruction[0].timing.repeat.boundsPeriod.start`, else `authoredOn`;
    - stop is `boundsPeriod.end`, else unknown, and never inferred from status;
    - `entered-in-error` is dropped;
    - the rule is stated in the spec and in the trial card's known failure modes;
    - B21 clinician review is still owed.
40. **(23h) Attachments.** `GuardedHttp` refuses anything but JSON (`fhir_client.py:182-184`),
    and the adapter is pure. Default:
    - Binary is read as FHIR JSON in the CDS and SMART layers, under the fhirServer base only;
    - text/plain and text/html are read; RTF, PDF and CDA are unreadable;
    - at most 20 reads of 1 MiB each per decision;
    - `fhir/decide` and `$decide` never fetch;
    - Epic often uses RTF, so the trial and prior-auth packs may abstain often, and the coverage
      counts show it (owner decision 9).
41. **(23h) The payer.** Default: `payor[0].reference`, then the Organization identifier by
    system, then the site map. A 0.2 pack keeps the literal identifier.
42. **(23i) A site code map without touching the pack digest.** Default:
    - a translation from a site's (system, code) into one of the pack's declared codes, done in
      the adapter;
    - logic files and digests are untouched;
    - the map is stored in the environment, per organization, and each decision records its
      version and sha256 (owner decision 2);
    - a site's own SNOMED CT codes may sit in its configuration under its licence, but shipped
      examples are fictional.
43. **(23i) Automation through a site code.** Definition of shipped §7 forbids configuration
    from producing AUTO that it could not before. Default: a `site_code` cap in `core/policy.py`
    turns those decisions into HUMAN_REVIEW. Lifting it is the owner's call after G5-6
    `site-validation`. Plan B had no cap.
44. **(23i, 23j, 23k) Pack rebuilds.** A policy or pack change needs
    `./scripts/build-reference-pack.sh`. Default: 23i reruns it and expects no diff; 23j and 23k
    commit their regenerated artifacts together.
45. **(23j) The inbox pack.** Excluding it from hooks would break `smart_routes.decide_for`,
    which reads its CDS prefetch (`:457-458`), and many fixtures. `CdsCard.summaries` is keyed
    by label and needs at least one entry (`spec.py:574`), so a summary per team would lose the
    outcome verb ("Routed" against "Review"). Default:
    - a template with indicators per team, a `none` summary and a suggestion;
    - team summaries keep the default outcome wording, with display names;
    - it stays off Inferno's list;
    - `test_the_inbox_card_wording_is_unchanged` changes deliberately in 23f and 23j.
46. **(23j) The referral `show` list** is a clinical-workflow judgement (owner decision 4).
    Default: `[return_for_info, urgent_review]`. These are the labels where the person signing
    the order can act; a coordinator or the office handles the others.
47. **(23k) Clinical behaviour on FHIR input.** The owner accepted on 2026-10-08 that LP-23's
    selector fixes the trial pack's first-coding miss. The medication, payer, notes and referral
    changes are new. Default: build 23k and hold the merge for the owner's go-ahead (owner
    decision 5).
48. **(23l) Packaging Inferno.** The kit publishes no image, and its own compose files use
    unpinned images. Default:
    - Mendari's own `sandbox/inferno/Dockerfile`, with the base pinned by digest and the kit at
      its commit SHA;
    - an overlay with every image pinned by digest;
    - `COMPOSE_FILES` and the Dockerfile pin test are extended to cover them.
49. **(23l) Network needs and required checks.** The resource validator fetches IGs from
    packages.fhir.org and validates with tx.fhir.org, and the Definition of shipped needs every
    `ci.yml` job green. Default:
    - a separate, report-only `conformance.yml`, and the acceptance is a green run of it;
    - making it required is the owner's GitHub setting (owner decision 10).
50. **(23l) Test 1.01 needs TLS; the prompt says "over HTTP".** Default: 1.01 is an expected
    failure in the golden, with its reason. TLS belongs to the deployment.
51. **(23l) No v2.2.1 execution script ships with the kit.** Only v2.0.1 `client_vs_server` does
    (checked). Default: Mendari writes the session and its golden, and any difference fails.
52. **(23l) What Inferno tests about auth.** `invoke_hook_with_bad_auth.rb` sets
    `fhirAuthorization.access_token` to `TRIGGER_500_STATUS`, so Inferno's FHIR server answers
    500 for the Technical Issues group. It is not a bad-JWT test, as Plan B said. Default:
    token refusal is proven by Mendari's own tests, and Inferno proves the ABSTAIN path and the
    `indeterminate` coverage.
53. **(23l) CRD's standard prefetch forms.** `fill_template` cannot fill nested tokens such as
    `{{context.draftOrders…}}` (`cds_hooks.py:174-187`), and Mendari uses `Coverage?patient=`
    where CRD uses `Coverage?member=`. Default: the packs do not change, and these are expected
    warnings in the golden.
54. **(23l) A terminology server in CI, against "never call a live service from a PR".**
    Default: a scheduled job only, while the offline `fhir-conformance` stays the required gate.
55. **(23l) The SMART client suite may need a person to start the launch.** Default: try it
    headless; if that fails, record it in its golden and run it by hand.
56. **(23l) The vendor scope check.** Default: build the comparison with `scopes_supported` and
    run it against smart-launcher. "Not published" is reported as `unknown`, never assumed. The
    run against Epic is the owner's.
57. **(23m) Closing the gate.** `a12-ehr-pilot` is `owner: build`, and `check_gates.py:139-140`
    refuses an attestation on a build prerequisite. `tests/test_gates.py` pins G5-6 at 6
    prerequisites and the file at 17 attestations. Default:
    - replace the LP-23 pending probe with the tests;
    - add one owner `pending:` line for the Epic run, as the monitor gate's LP-21n line does;
    - no count changes, and the gate table shows what waits on the owner (owner decision 14).
58. **Requirement 8: refresh tokens or iframe mode.** Both are refused today: `smart.py:161-166`,
    ADR-0006:51-55, `X-Frame-Options: DENY`, `frame-ancestors 'none'`, COEP and COOP, and
    SameSite=Lax. Default:
    - nothing is built;
    - the checklist asks what Epic needs;
    - the scorecard's "vendor fit" row says it waits on the owner;
    - ADR-0006 changes only after the sandbox shows the need (owner decision 13).
59. **(23a) Epic's free sandbox may not be able to call an external CDS service at all.** This is
    unverified. Default: the checklist asks. If it cannot, the Epic path is proven with test keys
    and Inferno only, until a pilot site's environment exists, and the scorecard says so (owner
    decision 15).
60. **ADR and threat-row numbers.** Other lanes built at the same time (LP-11o, LP-18h, LP-17) take
    other numbers. Default: the allocation at the top of this plan. `tests/test_threat_model.py`
    catches duplicates.

## Owner decisions still open

1. **CDS client registry storage.** Default: `MENDARI_CDS_CLIENTS`, checked at startup, for the
   pilot. A database-backed, admin-managed, chained registry comes before a second EHR. It is not
   set in Production until a pilot is signed.
2. **Site code map storage, and whether site-mapped decisions may ever automate.** Default:
   `MENDARI_SITE_CODES` per organization, with the HUMAN_REVIEW cap kept until G5-6
   `site-validation`.
3. **The scorecard.** Default: approve rubric v1 and its lowest-dimension rule from 23a, or
   supply the original "2.5 of 5" document.
4. **The referral `show` list.** Default: `[return_for_info, urgent_review]`. 23j merges once
   you confirm it.
5. **The go-ahead for 23k**, including the MedicationRequest rule. Default: build it, and hold
   the merge for your go-ahead with a clinician's note on the rule (B21). The trial selector
   part was already accepted on 2026-10-08.
6. **Whether an unreadable ABSTAIN always shows a card.** Default: yes, an `info` card, whatever
   `show` says.
7. **The feedback comment.** Default: dropped. The alternative is a note kept apart from the
   chain, sealed under `encrypt` and purged by retention.
8. **Override reason codes and their wording.** Default: three neutral codes: not relevant,
   already addressed, clinical judgement.
9. **Attachment rules.** Default: plain text and HTML only, at most 20 reads of 1 MiB each, and
   RTF later if the sandbox shows Epic uses it widely.
10. **Making the conformance jobs required.** Default: after four consecutive green weekly runs
    on main. This is a ruleset change.
11. **The engine trace behind Mendari sign-in.** Default: accept it. Pilot clinicians get Mendari
    accounts or use the SMART view.
12. **The form of the card's launch link.** Default: `?pack=`, checked in the sandbox.
13. **Requirement 8 after the sandbox.** Default: neither refresh tokens nor iframe mode unless
    Epic needs them. Any change amends ADR-0006.
14. **The gate.** Default: the Epic run is an owner `pending:` line on `a12-ehr-pilot`. The
    alternative is a seventh, owner-attested G5-6 prerequisite, which changes two pinned counts
    in `tests/test_gates.py` and the audited gate list.
15. **If Epic's free sandbox cannot call an external CDS service.** Default: a pilot site's own
    environment, with Oracle Health's sandbox as the backup. Until then the Epic path is proven
    with test keys and Inferno only.

Owner actions that are not decisions:
- Sign up for Epic's sandbox from `notes/epic-sandbox-checklist.md`, and record the iss, jku,
  aud form, sub and tenant, feedback support, scopes and launch handling.
- Put Epic's JWKS origin and FHIR base on `MENDARI_FHIR_ALLOWED_SERVERS`.
- Set `MENDARI_CDS_CLIENTS` (and any site codes) as Production-only, once a pilot is signed.
- Run `mendari smart check` against Epic.
- Do the Epic sandbox run that lets a build PR remove the gate's pending line.

## Risks

- **The open-mode shortcut.** Any CDS logic placed below `auth.py:1062` silently never runs in
  open mode. 23b's and 23c's tests pin the branch's position.
- **A suite-wide sandbox flag** could hide a regression in the confinement.
  `tests/test_cds_open_mode.py` must unset it, and `test_deploy` pins which compose services set
  it.
- **A vacuous verifier.** If the registry or the test keys are missing, the token tests would
  pass on nothing. Each one asserts that the registry is on and the principal kind is `cds`.
- **The catch-all rule.** The feedback route needs its explicit rule before the catch-all, or it
  falls to `admin`. `test_every_route_has_the_permission_it_should` pins it.
- **Literal event kinds.** `record_event` must be called with the literal `"card.feedback"`
  (`recorded_kinds`).
- **Route sweeps.** `tests/test_evalset_leaks.py` asserts that every route is called (`:1501`),
  so a new route that is not added there fails the suite.
- **JWKS amplification.** Random kids could trigger repeated refetches; the per-client minute
  limits that.
- **Linked-read volume.** The per-decision cap limits it.
- **Rate limits per EHR.** One `cds:<id>` principal carries a whole hospital: 600 decisions a
  minute with bursts of 100 (`limits.py:112`), plus `MENDARI_PRINCIPAL_DAILY_LIMIT`. Pilot
  volume should be measured before go-live.
- **Summaries growing past 140 characters** with display names. 23f checks every expansion at
  load.
- **Abstention on real Epic data** (RTF notes, many attachments). The strict rule may abstain
  often. The data-coverage counts make that visible.
- **Golden churn.** The Inferno golden changes across 23f, 23k and 23l. Each PR regenerates it,
  and review checks that every changed id has its reason.
- **Upstream changes.** Kit and inferno-core releases can change results. Pin by commit SHA and
  upgrade on purpose.
- **The Postgres DDL rule.** No table is added, which avoids it.

Checked against main (06d5332f):
- **Ids and open PRs.**
  - No PRs are open on mendari-hq/mendari.
  - The next free ADR was ADR-0013 (ADR-0012 exists); LP-17 takes it, so LP-23's is ADR-0014.
  - The highest threat-model rows are S17, T6, R2, I13, D10 and E11, so the next free are S18,
    T7, R3, I14, D11 and E12.
- **Auth and origin.**
  - `_BEARER` is at `auth.py:555`.
  - `Auth.check` returns at `:1062` before `authenticate`.
  - The CDS scope rules are at `scopes.py:182` and `:188`, not `:174` and `:179` as the code map
    said.
  - `OriginCheck` lets bearer requests through only with sign-in on (`origin.py:73-74`), and
    `SAFE_METHODS` is GET and HEAD only (`:54`).
  - Starlette's TestClient defaults to `client=("testclient", 50000)`.
- **Store and gates.**
  - `smart_claim` (`audit.py:2792`) has no purge; `smart_put` purges (`:2665-2672`).
  - With sign-in on, decisions chain `requested_by`.
  - `tests/test_gates.py` pins G5-6 at 6 prerequisites and the file at 17 attestations.
  - A build prerequisite already carries an owner `pending:` line (the monitor gate, LP-21n).
- **Tests the plan changes or keeps.**
  - `test_without_a_fhir_server_prefetch_is_still_required` and
    `test_a_server_off_the_list_is_refused` both assert 422.
  - `test_cds_requires_prefetch` matches `FhirError` with "prefetch required".
  - `test_the_inbox_card_wording_is_unchanged` pins the inbox's wording.
  - `CdsCard.summaries` needs at least one entry.
- **Card and errors.**
  - The card prints a bare confidence percentage (`cds_hooks.py:222-225`), and no test pins it.
  - `_STATUS` takes the first match (`app.py:150-165`).
- **The Inferno CRD kit.**
  - It is at v0.14.2 (2026-09-09).
  - `jwt_helper.rb` sends the header `{alg, typ: JWT, kid, jku}` and the payload
    `{iss, aud, exp, iat, jti}`.
  - The Technical Issues group requires `indeterminate`, `technical` and a reason text.
  - Execution scripts ship for v2.0.1 only.
- **Corrections to the drafts:**
  - Plan B misread `invoke_hook_with_bad_auth.rb` (conflict 52).
  - Plan B had no cap on site-code automation (conflict 43).
  - Plan B let ABSTAIN short-circuit past BLOCK (conflict 20).
  - Plan B added an owner prerequisite, which changes two pinned counts (conflict 57).
  - Plan A abstained on a server off the allow-list (conflict 14).
  - Plan A let `show` hide an unreadable ABSTAIN (conflict 25).
  - The drafts disagreed on the MedicationRequest start (conflict 39).
  - Neither draft caught the card's bare percentage (conflict 30) or the leak sweep's
    every-route assertion.
  - Plan A's 21 PRs and Plan B's 19 are consolidated into 13. The guards stay in the PRs they
    guard: the verifier with the principal, the origin checks with CORS, and the cap with the
    site map.