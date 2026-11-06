# ADR-0014: A CDS client is a principal, and a record it cannot read abstains

## Status
Accepted (2026-11-06, LP-23m). Proposed 2026-10-17. It amends
[ADR-0006](ADR-0006-an-ehr-launch-is-a-principal.md)'s rule that the narrow EHR principal exists
only for SMART launches. Built in steps (LP-23, `tasks/todo.md`, planned in
`notes/lp23-plan.md`). What landed, in the order it was built:
- 23a (#256), this record, the plan, the Epic sandbox checklist
  (`notes/epic-sandbox-checklist.md`), the EHR scorecard (`notes/ehr-scorecard.md`) and a golden
  of today's FHIR decisions;
- 23b (#262), decisions 1 to 4: the registry of CDS clients, the token verifier and the `cds`
  principal;
- 23d (#264), decisions 7 and 8: a recorded ABSTAIN on an unreadable record, and the
  data-coverage record;
- 23c (#269), decisions 5 and 6: open mode on this machine only, and CORS for registered origins;
  23f (#270), cards and card feedback as CDS Hooks 2.0 and Da Vinci CRD expect (the card's `uuid`
  its decision's id, `selectionBehavior`, three neutral override reasons, and
  `POST /cds-services/{id}/feedback` chained as `card.feedback`); and 23g (#271), the FHIR
  mapping syntax 0.3, which no shipped pack adopts yet. 23c and 23f landed on main with 23g,
  through #271;
- 23e (#280, the LP-23 lane), a vendor-shaped synthetic EHR profile (`sandbox/ehr_profile.py`)
  and a signed sandbox CDS client over HTTP (`mendari sandbox cds-client`), which compose runs
  against both engines;
- 23h (#280), linked record data under the EHR's FHIR server: a note by its Binary link, the
  payer and the medication by reference, at most 20 reads a decision;
- 23i (#280), a site code map per organization (`MENDARI_SITE_CODES`), versioned, its digest in
  each decision's record, and a decision that used it never automated;
- 23l (#280), conformance outside the unit tests: Inferno's CRD server, SMART client and Bulk
  client suites (`conformance.yml`), the HL7 validator with a terminology server on a schedule
  (`terminology.yml`), and `mendari smart check` against the EHR's published scopes;
- 23m (#280), the close-out: gate `a12-ehr-pilot`'s probes, the threat model's residual
  risks, this status, the scorecard graded again, and the end-to-end test
  (`tests/test_ehr_pilot_e2e.py`);
- 23j (#281), after the close-out, the shipped packs' cards: the inbox's own
  card template, and referral cards only when the person signing can act (`show:
  [return_for_info, urgent_review]`, the default the owner confirmed on 2026-11-06). No
  decision changes.

What stays open. 23k (the reference packs' mappings for real EHR data, on spec 0.3) waits on the
owner's go-ahead, with a clinician's note on the MedicationRequest rule (B21). The Epic sandbox
run, in which every pack with a standard hook decides on Epic's sandbox patients (A12.acc-3), and
`mendari smart check` against Epic are the owner's. Gate G5-6's `a12-ehr-pilot` names both as
pending. `conformance.yml`'s new jobs
(`crd-server`, `smart-client`, `bulk-client`) have not run on GitHub yet (its `dtr-payer` job
already runs weekly on main), and `terminology.yml` has not run at all: their first runs on main
are recorded in the `tasks/todo.md` build log after they happen. Refresh tokens and iframe mode
stay unbuilt until the sandbox shows that Epic needs them. The defaults below hold unless the
owner chooses otherwise: the owner's decisions still open are listed in `tasks/todo.md`.

What the first three steps built, with the details they settled:

- **23b (2026-10-18):** decisions 1 to 4, in `server/cds_clients.py` (the registry, the key
  cache and the verifier) and `server/auth.py` (the `cds` principal and the CDS branch of the
  guard). Two details the decisions leave open are settled there. A JWT-shaped credential is a
  `Bearer` credential that holds a `.`, which none Mendari issues does, so a JWT of any length
  reaches the verifier on `/cds-services`, and one over 8 KiB is refused by its size. The `jti`
  claims are the session table's kind `cds-jti`, kept apart from Backend Services' `jti` and
  never ended by the break-glass; each claim now deletes up to 100 expired rows, as each put
  does. A `cds` principal in open mode acts for `default` and is named in the chain
  (`requested_by: cds <id>`), the one principal open mode has. Three points differ from what
  23a first wrote below, and the decisions now say so. A `cds` token off `/cds-services` is
  never read as a credential, so it is not the `403` decision 3 first said: with sign-in on it
  is the one `401` (`malformed` or `unknown_prefix`), and in open mode it is ignored, as every
  credential is there; only a `cds` principal found off `/cds-services`, which nothing makes
  today, is a `403` (`cds_only`). The `jti` is claimed after the guard's own checks too (the
  organization's record in memberships mode, the route's permission), so a token refused for
  either claims nothing. And the key cache is each instance's own, so "once per client a
  minute" is per instance.
- **23c (2026-10-18):** decisions 5 and 6, in `server/cds_clients.py` (`on_this_machine`,
  `CdsOpenMode`, `cds_sandbox`, and the registry's `origins` and `from_its_origin`),
  `server/auth.py` (the open-mode rule, first in the CDS branch, and the client's origin checked
  before its `jti` is claimed), `server/cors.py` (new) and `server/origin.py`. Four details the
  decisions leave open are settled there. `X-Forwarded-Host` and `X-Real-IP` are forwarding
  headers too, beside `Forwarded` and `X-Forwarded-For`. `MENDARI_CDS_SANDBOX=0` is off, as
  unset is, and any value but `1` or `0` stops startup. Every answer on `/cds-services` and its
  children carries `Vary: Origin`, not only an answer to a registered origin. A registered origin
  passes the cross-site check (`OriginCheck`) only without a Mendari cookie, so a page there
  never calls as the person signed in. Three points differ from what 23a first wrote below, and
  the decisions now say so. A registered origin's preflight is answered before the open-mode
  rule too, in either mode and from any machine: it carries no credential and returns nothing,
  and the call that follows meets the rule. A `cds` token from a page on this site's own origins
  (the request's host, `MENDARI_PUBLIC_URL`, `MENDARI_TRUSTED_ORIGINS`) passes the client's
  origin check, as one from no page does. And the test suite does not set the flag: it runs its
  servers as if the flag were set wherever it may be, so no server a test starts in a
  subprocess inherits it. A proxy on this machine that adds no forwarding header, nginx's
  default among them, makes every call it forwards look local: behind any proxy, sign-in goes on
  (threat E16).
- **23d (2026-10-18):** decisions 7 and 8: a prefetch key that could not be read, or a note that
  is only a link or does not decode, gives a recorded ABSTAIN with its reason
  (`errors.ReadReason`), an `info` card whatever `show` says, and CRD's `indeterminate` coverage
  when the Coverage was read; neither prefetch nor `fhirServer` is a `412`, and request faults
  stay `4xx`. Every FHIR decision's record carries `data_coverage` (`standards/data_coverage.py`).
  The SMART launch's decide reads its record the same way.

## Context
An EHR calls a CDS Hooks service with its own signed token. Mendari cannot take one today:

- **With sign-in on**, `/cds-services` takes only Mendari's own credentials: a key scoped to
  decide (the `cds-hooks` preset) or a SMART Backend Services client's token. An EHR's signed
  JWT is a credential Mendari did not issue, and it is refused before it is read (bearer
  credentials are capped at 512 characters, and a real RS384 token is longer).
- **In open mode** the `Authorization` header is never read, and open mode answers on any host,
  so a forged JWT gets cards (vision audit, A12.ctx-jwt-today).
- **The narrow EHR principal** exists only for SMART launches (ADR-0006): a launch cookie on the
  launch's own routes, with the `launch` and `launch_decide` permissions. A hook has no launch.
- **No CORS:** a CDS client running in a browser on the EHR's origin cannot read an answer.
- **A failed read fails the hook.** A prefetch read that fails is a `422` and no decision is
  recorded (A12.3f). A note given only as a link, or whose data does not decode, reads silently
  as no notes (A12.3a).

The pilot EHR is Epic, with Oracle Health as the backup (`tasks/todo.md`, B38). Epic publishes
its CDS Hooks token as RS384 or ES384, with a `jku` equal to an allow-listed URL, `iss` the FHIR
base, `aud`, `sub` and a `jti`. CDS Hooks 2.0 defines `iss`, `aud`, `exp`, `iat` and `jti`, and
Inferno's Da Vinci CRD kit sends exactly those, with a header of `alg`, `typ`, `kid` and `jku`.

## Decision
1. **A registry of CDS clients (23b).** `MENDARI_CDS_CLIENTS` is a JSON list, checked at startup
   as `MENDARI_SMART_EHRS` is. Each client has an id, a title, its `iss`, its organization, the
   `fhirServer` bases it may send and the origins it calls from (for CORS, 23c). Its keys are
   given by value (public keys only, RSA or EC P-384, checked as Backend Services checks them) or
   by one pinned `jwks_uri` whose origin is on `MENDARI_FHIR_ALLOWED_SERVERS`, so one allow-list
   governs every outbound call to an EHR. A client may also pin `sub` and `tenant`, and require
   `iss` to be the `fhirServer` it sends. Unset, the feature is off and no JWT is accepted
   anywhere. A startup refusal names the setting and the entry's position, never key material.
   Changing the registry means a redeploy; a registry kept in the database, managed by an
   administrator and chained, comes before a second EHR.
2. **What a token must be (23b).**
   - RS384 or ES384, at most 8 KiB. The header holds only `alg`, `kid`, `typ` (`JWT`) and
     `jku`. `jku` must equal the registered `jwks_uri` exactly; a client whose keys are given by
     value takes none. Exactly one key matches the `kid`.
   - `iss`, `aud`, `exp`, `iat` and `jti` are required. `aud` is `MENDARI_PUBLIC_URL` plus the
     request's path, compared exactly and never with the `Host` header; a registry without the
     public URL stops startup. The leeway is 30 seconds, `iat` is not in the future, and a token
     lives at most 330 seconds.
   - `sub` and `tenant` must equal the registered values when those are set. A `tenant` claim
     is refused when none is registered: an Epic shared by several organizations (Community
     Connect) is not supported.
   - A `jti` is claimed once per client, last (after the organization's record and the
     route's permission are checked), so a refused token claims nothing.
   - Keys are fetched only through the allow-listed client and kept 3,600 seconds. An unknown
     `kid` fetches the keys again at most once per client a minute on each instance, and an
     answer that holds a private key is refused and keeps nothing.
   - Each refusal has its reason from a closed set. The security log names it `cds_<reason>`
     with the caller `cds:<id>`, never a claim's value or the token.
3. **The `cds` principal (23b).** A verified token is a principal of kind `cds` (`cds:<id>`,
   the actor `cds <id>`) in the client's registered organization. It holds exactly two
   permissions, `discover` and `decide`, and reaches `/cds-services` and its children only, as a
   launch reaches only the launch's routes: anywhere else its token is not a credential (decision
   4), and a `cds` principal found anywhere else gets `403`. The `fhirServer` it sends must be
   one of its registered bases, else `403` before anything is charged or fetched. It is rate
   limited and capped daily like any principal, by `cds:<id>`. The notice
   acknowledgement covers people only, so it is not asked. "The same credential reads
   discovery" means the same registered client and keys: a `jti` is good for one request.
4. **Verification in every mode (23b).** A JWT-shaped bearer credential on `/cds-services` is
   verified whether sign-in is on or off: the check runs before open mode's shortcut. Without a
   registry it gets `401`. With sign-in off, every registered organization must be `default`. On
   any other path a JWT is what it was before: with sign-in on, refused as a credential Mendari
   did not issue; in open mode, ignored, as every credential is there. Mendari's own keys and
   tokens do not change.
5. **Open mode answers on this machine only (23c).** With sign-in off, `/cds-services` answers a
   request only when it is local (a loopback client address, a local `Host` and no forwarding
   header) or when `MENDARI_CDS_SANDBOX=1` is set. That flag stops startup on Vercel or with
   sign-in on. Only the compose sandbox's open engine sets it, and the test suite runs its
   servers as if it were set wherever it may be, without setting the variable. A verified token
   does not lift the rule. A registered origin's preflight (decision 6) is answered before it,
   from any machine: it carries no credential and returns nothing.
6. **CORS for registered origins (23c).** On `/cds-services` and its children only, for the exact
   origins the registry lists, and never with credentials. A preflight from a registered origin
   is answered before the guard, and every answer there varies by `Origin`. A `cds` token from a
   page must come from one of that client's own origins or from this site's own (the request's
   host, `MENDARI_PUBLIC_URL`, `MENDARI_TRUSTED_ORIGINS`). Any other origin gets no CORS header
   and is refused as before.
7. **A record that cannot be read abstains (23d).** When a declared prefetch key could not be
   read (a failed fetch, an OperationOutcome, a structure error in a resource Mendari fetched
   itself, a note whose data does not decode or that is only a link), the decision is ABSTAIN,
   before any provider runs. Its reasons read `could not read <key>: <reason>`, from a closed set
   of reasons, never a URL, a query or a server's text. The decision is recorded, and the hook
   answers `200` with an `info` card whatever the pack's `show` says. Input that is refused still
   BLOCKs; a request fault stays a `4xx`; neither prefetch nor `fhirServer` is a `412`, as CDS
   Hooks says. A server off the allow-list, or one the client did not register, is
   configuration, not data: it is refused, with nothing fetched or recorded.
8. **The data-coverage record (23d).** Every FHIR decision (CDS Hooks, `fhir/decide`,
   `$decide`, the SMART launch's, DTR) carries `data_coverage` (`mendari.data-coverage/1`). For
   each mapped field and list it holds the paths looked for, the declared code systems and the
   pack's own looked-for codes, the resources counted by type, the values found, the codings
   counted by declared system (any other system as one count) and the unreadable keys counted
   by reason. Counts and declared codes only: never a value, an id, text, a patient's code or an
   undeclared system. A JSON decision carries none, and older records still verify.

ABSTAIN only lowers automation, and no part of this decision raises it. Refresh tokens and iframe
mode stay deferred, as ADR-0006 says, until the Epic sandbox shows what Epic needs; any change
there amends ADR-0006.

## Consequences
- An EHR calls with its own token. Each call is attributable to its registered client, limited
  per principal, contained in one organization, and reaches nothing but `/cds-services`.
- Open mode reached over a network stops answering CDS Hooks. That is a tightening, and the
  CHANGELOG names it when 23c lands. The public CDS Hooks Sandbox, which sends its own JWT, has
  to be registered even for a demo on this machine.
- One `cds:<id>` principal carries a whole site's hooks under one rate limit and one daily cap,
  so a pilot's volume is measured before it goes live.
- A record Mendari cannot read goes to a person, never to a guess. On note-heavy EHR data (RTF
  notes, for one) the packs may abstain often, and the data-coverage counts show it.
- The only thing stored is the `jti` claims, which expire. Rolling back past 23b turns an EHR's
  tokens back into refusals.
- The [threat model](../security/threat-model.md#residual-risks-and-assumptions) keeps five
  residual risks of this design: trust in the EHR's key and its rotation, a compromised `jku`
  host, the decision id disclosed to the EHR, a misconfigured site map, and frequent abstention
  on RTF-heavy records.

## Alternatives Considered
- **An EHR's token as a Mendari key.** Rejected: a key is Mendari's secret, handed to the EHR;
  the EHR's own key pair never leaves the EHR, and its tokens live minutes.
- **The SMART launch's principal for hooks too.** Rejected: its permissions read through a
  launch's own session, and a hook has none.
- **Verifying tokens only with sign-in on.** Rejected: open mode answered a forged token, and no
  unverified JWT is to be accepted in any mode.
- **Abstaining on a server off the allow-list.** Rejected: that is configuration, not the
  patient's data. It stays a refusal, with nothing fetched or recorded.
- **Abstaining before the input schema runs.** Rejected: it would turn some refusals (BLOCK)
  into abstentions.
- **An `aud` of the base URL alone.** Not built until Epic's sandbox shows it sends one.
