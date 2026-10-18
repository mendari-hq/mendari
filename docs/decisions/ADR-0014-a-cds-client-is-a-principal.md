# ADR-0014: A CDS client is a principal, and a record it cannot read abstains

## Status
Proposed (2026-10-17). It amends [ADR-0006](ADR-0006-an-ehr-launch-is-a-principal.md)'s rule that
the narrow EHR principal exists only for SMART launches. It is built in steps (LP-23,
`tasks/todo.md`), and each step records here what it built when it lands:
- 23b, the registry of CDS clients, the token verifier and the `cds` principal;
- 23c, open mode on this machine only, and CORS for registered origins;
- 23d, a recorded ABSTAIN on an unreadable record, and the data-coverage record.

23a wrote this record and the plan. What each step has built since:

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

The defaults below hold unless the owner chooses otherwise.

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
   sign-in on, and only the compose sandbox's engine and the test suite set it. A verified token
   does not lift the rule.
6. **CORS for registered origins (23c).** On `/cds-services` and its children only, for the exact
   origins the registry lists, and never with credentials. A preflight from a registered origin
   is answered before the guard. A cross-site `Origin` on a `cds` token must be one of that
   client's own origins. Any other origin gets no CORS header and is refused as before.
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
