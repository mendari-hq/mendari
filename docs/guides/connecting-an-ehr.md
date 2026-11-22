# Connecting an EHR

An EHR calls Mendari's CDS Hooks services (`GET /cds-services`, `POST /cds-services/{id}` and
its card feedback, `POST /cds-services/{id}/feedback`) in one of two ways:

- **With a Mendari key.** An administrator makes a key from the `cds-hooks` preset and the
  EHR sends it as `Authorization: Bearer ohk_…`
  ([the presets](../deploying-with-phi.md#2-sign-in-and-roles)). This needs sign-in on.
- **With its own signed token**, as CDS Hooks 2.0 describes ("Trusting CDS Clients"). You
  register the EHR once, and each call carries a short-lived JWT the EHR signs with its own
  private key. Mendari never holds that key. This page is about this way.

Off until you register a client: with `MENDARI_CDS_CLIENTS` unset, a JWT on `/cds-services` is
refused in every mode, open mode included, and everything else works as before. Registered or
not, with sign-in off the CDS services answer this machine only ([In open mode](#in-open-mode)).
The decision behind it is
[ADR-0014](../decisions/ADR-0014-a-cds-client-is-a-principal.md).

!!! note "Which agents this page names"
    The bundled demo agent, `mendari/portal-message-agent`, offers one CDS Hooks service, the
    hook `mendari-message-received` (a FHIR `Communication` in, a card and a `Task` out). The
    Clinical Trial Prescreener, the Prior Authorization Preflight, the Referral Router and the
    Patient Inbox Router that this page also names are the former example agents: they remain
    as test fixtures (`tests/fixtures/reference-packs/`) and on the branch
    `archive/decision-packs`, and what is said of them describes how the engine treats an agent
    of that shape.

## Register the EHR

`MENDARI_CDS_CLIENTS` is a JSON list, one entry per CDS client. It is read at startup, so a
change takes effect at the next restart or redeploy. A sandbox EHR, for example (the names are
fictional):

```json
[
  {
    "id": "ehr-sandbox",
    "title": "EHR sandbox",
    "iss": "https://ehr.example.org/fhir/r4",
    "org": "default",
    "fhir_servers": ["https://ehr.example.org/fhir/r4"],
    "jwks_uri": "https://ehr.example.org/.well-known/jwks.json",
    "iss_is_fhir_server": true
  }
]
```

| Field | Required | What it is |
|---|---|---|
| `id` | yes | Lower-case letters, digits and hyphens, at most 63. The client is the principal `cds:<id>`, and the audit chain names it `cds <id>`. |
| `title` | yes | A name for people, at most 100 characters. |
| `iss` | yes | Exactly what the EHR's tokens carry as `iss`. Some EHRs send their FHIR base; Inferno's CRD kit sends its suite's own URL, `<its host>/custom/crd_server_v221`, with `jku` that URL plus `/jwks.json` ([Conformance runs](#conformance-runs)). |
| `org` | yes | The organization its calls act for, and whose decisions they become. With sign-in off it must be `default`. In memberships mode it needs a record. |
| `fhir_servers` | yes | The FHIR bases (1 to 20) the EHR may send as `fhirServer`. Each must have its origin on `MENDARI_FHIR_ALLOWED_SERVERS`, and if `MENDARI_SMART_EHRS` registers the same base for launches, it must be to the same organization. |
| `jwks` or `jwks_uri` | one of the two | The EHR's public keys. `jwks` is a JWK Set by value. `jwks_uri` is the one URL Mendari fetches them from: https (plain http only for a loopback or single-label host), with its origin on `MENDARI_FHIR_ALLOWED_SERVERS`. |
| `sub` | no | When set, every token must carry exactly this `sub`. |
| `tenant` | no | When set, every token must carry exactly this `tenant`. When not set, a token with any `tenant` is refused: an EHR shared by several organizations is not supported yet. |
| `iss_is_fhir_server` | no | `true`: the `fhirServer` a call sends must also be its `iss` (which must then be one of `fhir_servers`). |
| `origins` | no | The browser origins (`https://host[:port]`, 0 to 20) the EHR's pages call from. A page there may call the CDS services and read their answers ([below](#from-a-browser)); a page anywhere else may not use this client's tokens. |

Also set `MENDARI_PUBLIC_URL` to the address the EHR calls. A token's `aud` is that URL plus the
path called, so without it the server does not start. It is https, or plain http only for a
loopback address or a single-label host (a compose service, such as the sandbox's
`http://engine-signin:8000`).

Anything wrong in the list stops the server at startup. The message names the setting, the
entry's position and the field, for example
`MENDARI_CDS_CLIENTS entry 2: fhir_servers 1 is not on MENDARI_FHIR_ALLOWED_SERVERS`. It never
shows a value or any key material.

## The keys

Keys are RS384 (RSA of 2048 to 8192 bits) or ES384 (EC P-384) public keys, 1 to 10 in a set,
each with its own `kid`. A set that holds a private or symmetric key is refused, by value at
startup and from a `jwks_uri` when it is fetched: treat such a key as exposed.

Keys from a `jwks_uri`:

- are fetched through the same allow-listed client as every other call to the EHR: no
  redirects, a timeout and a size cap, and a JSON answer only (`application/json`);
- are kept an hour;
- are fetched again, at most once a minute per client, when a token names a `kid` the kept keys
  do not hold. A key the EHR rotates in is found within a minute of its first use. A flood of
  made-up `kid`s costs the EHR one request a minute from each running instance of Mendari.

Each instance keeps its own keys (on Vercel, each function instance), so a new instance fetches
them at its first token, and the limit above holds per instance: with several running, the EHR
sees up to one request a minute from each.

An answer that is not a set of public signing keys, a key of the wrong shape among them, keeps
nothing, and drops what was kept. A failed fetch (no answer, an error, or an answer that is not
JSON or is nested too deep to read) keeps what was kept until it is an hour old. Each is one
`cds.jwks.refused` line in the security log.

**Retiring a key, and trusting its host.** Mendari learns that the EHR retired a key only when it
fetches the keys again. A key taken out of the JWK Set still verifies for up to an hour on each
instance that kept it, unless a token under a `kid` it does not hold makes that instance fetch
sooner. A key given by value verifies until you change `MENDARI_CDS_CLIENTS` and redeploy, which
is also how to stop a client at once. A key the EHR rotates in is found at the next fetch: on an
instance that fetched in the last minute, its tokens are refused (`cds_key`) until that minute
has passed. Mendari trusts whatever public keys the `jwks_uri` serves, so whoever controls that
host, its DNS or a certificate for its name can publish one, and a successful fetch is not
logged. An EHR whose tokens carry no `jku` can be registered with its keys by value instead,
which takes the host out of the trust; a client registered so refuses any token that names a
`jku`.

## The token

The EHR signs one token for each call:

| Part | What Mendari requires |
|---|---|
| Size | At most 8 KiB, three base64url segments. |
| Header | Only `alg`, `kid`, `typ` and `jku`. `alg` is `RS384` or `ES384`, `typ` is `JWT`, and `kid` names one of the client's keys of that key type. `jku`, when sent, must be the registered `jwks_uri` exactly; a client whose keys are given by value sends none. |
| `iss` | The registered `iss`, exactly. |
| `aud` | One string: `MENDARI_PUBLIC_URL` and the path called, exactly. Discovery's is `https://<mendari>/cds-services`, a hook's `https://<mendari>/cds-services/<id>`. It is never compared with the `Host` header. |
| `exp`, `iat` | Required numbers. `iat` is not in the future, and `exp` is at most 330 seconds after `iat` and after now, with 30 seconds of clock skew. |
| `jti` | Required, 1 to 256 characters, used once per client. A token is good for one request: discovery and a hook each need their own. |
| `sub`, `tenant` | As registered (above). |

Every token that does not pass is the same answer, a `401` with
`WWW-Authenticate: Bearer realm="mendari", error="invalid_token"`. Nothing a refused token
sends is written: its `jti` is claimed only once every other check has passed, those of the
token, then (in memberships mode) its organization's record, then the route's permission. A
hook call refused for its `fhirServer` (below) has passed all of them, so its `jti` is used.

## What a client may do

A verified token is a principal of kind `cds`, in the client's registered organization. It may
list the services, call them and send feedback on their cards, and nothing else:

- It reaches `/cds-services` and its children only. On any other route the token is not a
  credential: with sign-in on the answer is the same `401` as for any other credential Mendari
  did not issue, and in open mode, which reads no credential there, it is ignored.
- The `fhirServer` it sends must be one of its `fhir_servers` (and its `iss`, with
  `iss_is_fhir_server`). Any other is a `403` before anything is fetched, charged or decided.
- It is rate limited and capped daily like any principal, as `cds:<id>`
  (`MENDARI_PRINCIPAL_DAILY_LIMIT`). One client carries a whole site's hooks, so measure a
  pilot's volume before it goes live.
- It is never asked to acknowledge the notice: that is for people.

With an `Authorization` header there is no fallback to a session cookie: the token decides
alone.

## From a browser

Some CDS clients run in the browser: a page on the EHR's own origin calls the services
directly. For that, list the page's origin in the client's `origins`. Then, on
`/cds-services` and its children only:

- The browser's preflight from that origin is answered before anything else, with `204`:
  `Access-Control-Allow-Origin` that origin, `Access-Control-Allow-Methods: GET, POST, OPTIONS`,
  `Access-Control-Allow-Headers: authorization, content-type`, `Access-Control-Max-Age: 600`.
- Every answer to that origin names it in `Access-Control-Allow-Origin`, a refusal included, so
  the page can read why. Every answer there carries `Vary: Origin`.
- `Access-Control-Allow-Credentials` is never sent. The page sends the EHR's token, never a
  cookie, and a call that carries a Mendari cookie is refused as another site's.
- A token from a page must come from one of its own client's `origins`, or from Mendari's own
  site. From any other origin, another client's included, the call is a `403`
  (`this CDS client does not call from this origin`) before the token's `jti` is used.

A page on an origin no client registered gets no CORS header, and its calls are refused as
before. Nothing else Mendari serves is open to other origins.

## In open mode

With sign-in off, the CDS services answer this machine only: a call from a loopback address
(`127.0.0.1`, `::1`), to a `Host` that names this machine (`localhost`, a `.localhost` name or a
loopback address), with no forwarding header (`Forwarded`, `X-Forwarded-For`,
`X-Forwarded-Host`, `X-Real-IP`). From anywhere else every call is a `403`, before any
credential is read, a valid token included, and the security log says `open_mode_remote`. An
EHR on a network calls a server with sign-in on, and so does anything behind a proxy. A proxy
that adds a forwarding header makes every call it forwards look remote. A proxy on this machine
that adds none makes every call it forwards look local, wherever it came from. nginx's default is
one: `proxy_pass http://127.0.0.1:8000` sends `Host: 127.0.0.1:8000` and no forwarding header.

A synthetic sandbox reached over a network, such as the compose sandbox's open engine, which
Docker reaches from its network's gateway, sets `MENDARI_CDS_SANDBOX=1`: it then answers from
anywhere, and says so at startup. `0` is off, as unset is. The server refuses to start with any
other value, and with `1` on Vercel or with sign-in on.

On this machine, a JWT on `/cds-services` is verified too, and without a registry it is
refused. A request with no credential is answered as before, so a local demo that sends no
token still works. Off `/cds-services` open mode reads no credential, a JWT included. A tool
that always sends its own JWT, such as the public CDS Hooks Sandbox, has to be registered first,
with `org` set to `default`, and its page's origin in `origins`.

## Check it

- Signed in, `/api/health` lists your organization's clients by id (`cds_clients`); `null`
  means none is registered.
- Each refusal is one `auth.credential.refused` line in the security log, with its reason and,
  once the token's `iss` names a registered client, `cds:<id>`. It never holds the token or a
  claim.

| Reason | What it means |
|---|---|
| `cds_unregistered` | A JWT came, and no client is registered here. |
| `cds_headers` | More than one `Authorization` header. |
| `cds_size`, `cds_format` | Over 8 KiB, or not three base64url segments of JSON. |
| `cds_header` | A header field other than `alg`, `kid`, `typ` and `jku`, an algorithm other than RS384 or ES384, `typ` not `JWT`, or no `kid`. |
| `cds_issuer` | No client is registered with this `iss`. |
| `cds_jku` | A `jku` other than the registered `jwks_uri`. |
| `cds_keys_unavailable` | The keys from the `jwks_uri` could not be fetched or were refused. |
| `cds_key` | No one key of the client matches the `kid` and the algorithm's key type. |
| `cds_signature` | The signature does not verify with that key. |
| `cds_audience` | `aud` is not the URL called. |
| `cds_expired`, `cds_not_yet_valid`, `cds_lifetime` | The token's time: expired, not valid yet (`nbf` or `iat` more than 30 seconds ahead), or living longer than 330 seconds. |
| `cds_claims` | A required claim is missing, or of the wrong type: `exp` or `iat` not a number (a numeric string included), or `jti` not a string. |
| `cds_subject`, `cds_tenant` | `sub` or `tenant` other than registered. |
| `cds_jti` | A `jti` that is empty or longer than 256 characters. |
| `cds_replayed` | The `jti` was used before by this client. |
| `cds_no_organization_record` | Memberships mode: the client's organization has no record. |

A `fhirServer` refused for a `cds` principal is an `auth.permission.refused` line, reason
`cds_fhir_server_not_registered`. A token from a page on an origin that is not its client's is
an `auth.origin.refused` line, reason `cds_origin`, with `cds:<id>`. In open mode, a call from
another machine is an `auth.permission.refused` line, reason `open_mode_remote`.

## Reading the record

A hook decides on its `prefetch`. A key the request leaves out (or sends empty) is read from
`fhirServer` with the `fhirAuthorization` token, only when that server is on
`MENDARI_FHIR_ALLOWED_SERVERS`; with no `fhirServer` the request is a `412`. A key whose value is
null has no data and is not read. When part of the record cannot be read, the hook still answers
`200`: the decision is ABSTAIN, its reasons say `could not read <key or field>: <reason>`, it is
recorded with its `data_coverage` record, and its card is an `info` card that names what was not
read.

### Linked data

An agent on spec 0.3 can read a note that is only a link to a Binary, and what a reference points
to: the payer behind `Coverage.payor`, the drug behind `medicationReference`
([the 0.3 mapping](../spec/decision-pack-0.3.md)). When the record does not hold them, Mendari
reads them from the same `fhirServer`, with the same token:

- one `GET` of one resource by `Type/id`, under the server's base URL, with
  `Accept: application/fhir+json`. The server must answer a Binary as FHIR JSON (its content in
  `data`), not as its raw bytes;
- at most 50 note Binaries and, apart from them, 50 followed references a decision, so a chart's
  notes never use up what its drugs need; each at most 1 MiB once decoded. A note field stops
  reading its notes once their text is longer than the agent's input takes: the decision abstains
  (`too_large`) whatever the rest hold;
- at most 100 reads a request, its decisions together (each draft order of an order hook is a
  decision), and 60 seconds spent in them. The decision's deadline (`MENDARI_DECISION_DEADLINE_S`)
  starts before its linked reads and its remote providers share what is left, so an EHR that
  answers slowly leaves the providers less time (they fail with `timeout`, and a person
  decides);
- each link once a request, however it is written (relative, or an absolute URL under the base),
  so the draft orders of one order hook share what was read;
- never a link to another server, or outside the base;
- nothing a MedicationRequest links to when the agent leaves it out of its list
  (`medication_period`: one entered in error, a proposal, a plan or an option, and an order not
  to give the drug).

A `fhirServer` that is not on `MENDARI_FHIR_ALLOWED_SERVERS` is configuration, not the patient's
data: for an agent that reads links, the hook is a `422` before anything is decided, with nothing
fetched or recorded, as when a prefetch key is left out. The token must allow reading what the
links name: `Binary`, and the types the agent follows a reference to. A link that is not read
makes the decision ABSTAIN with its field and one of these reasons:

| Reason | What it means |
|---|---|
| `off_server` | The link names another server, another scheme, or a path outside the base URL. Nothing was sent. |
| `cap` | The decision had already read 50 notes, or 50 followed references; or the request had sent 100 reads. Nothing was sent. |
| `timeout` | The read did not answer in time, or the decision's deadline or the request's 60 seconds of reads had run out (then nothing was sent). |
| `too_large` | A resource's answer, or a Binary's content once decoded, is over 1 MiB. |
| `not_fetched` | There was no server to read from (no `fhirServer`, `fhir/decide`, `$decide`), or the link names no `Type/id`. |
| `unsupported_type` | The link names another type than the agent reads (a note's url anything but a Binary, relative or absolute; nothing is sent), the Binary holds no content, or the note is of a type Mendari does not read. |
| `network`, `unauthorized`, `http_status`, `not_json`, `operation_outcome`, `not_r4` | The read failed. `not_json` is a Binary answered as raw bytes. |

Notes are read as plain text and HTML. RTF, PDF and CDA are not read, and the decision abstains,
unless another rendition of the same note was read: the `content` of one DocumentReference holds
one document in several formats, so an RTF beside the HTML of a note is not needed, and is not
read. A note none of whose renditions is read counts once, by the reason of its HTML or plain
text rendition (`cap`, a failed read), else `unsupported_type`, and the field's data-coverage
record counts the formats of the notes it read as `unsupported_type` (`unread_formats`: `rtf`,
`pdf`, `xml`, `image`, `none`, `other`). The record's `links` section says how many links the
decision read for each budget, and how many reads failed with each HTTP status. Since LP-23k the Clinical Trial Prescreener and the Prior Authorization Preflight read
links: their notes (a Binary), the Medication a MedicationRequest names (the trial agent), and
the Organization a Coverage's payor names (the prior-authorization agent). The token must allow
`Binary`, `Medication` and `Organization` reads for them. The Referral Router reads no link.

## The cards

Each card is CDS Hooks 2.0's:

- Its `uuid` is the id of the decision it shows. Its one suggestion has its own `uuid`, and the
  card says `selectionBehavior: at-most-one`. The EHR keeps the `uuid` beside the patient it
  sent, so an export or archive of the audit chain is identifiable beside the EHR's records,
  after Mendari's retention has removed the inputs too
  ([threat model](../security/threat-model.md#residual-risks-and-assumptions)).
- It offers three override reasons, from
  `https://mendari.co/fhir/CodeSystem/card-override-reasons` (served at
  `/fhir/CodeSystem/card-override-reasons`): `not-relevant`, `already-addressed` and
  `clinical-judgement`, each with its display.
- The summary uses the agent's display names, under 140 characters. The detail is the agent's
  status, the notice and one plain sentence for the outcome, and never a percentage: the
  confidence the card used to show as one is gone.
- Every decision is recorded, but an agent may show a card for some recommendations only (its
  `cds_hooks.card.show`; a decision with no recommendation, an ABSTAIN or a BLOCK, is its
  `none`, and since spec 0.3 one that refused its input is its `refused` where the agent names
  that key). An agent may also show a card for a recommendation it otherwise leaves out when a
  policy rule of its own matched, in that rule's words (`cds_hooks.card.also_show`). The
  referral router shows one only when the person signing can act: a referral to send back with
  what is missing, or one to review today. A referral to schedule, for a coordinator or with no
  suggestion gets no card, and the referral office works it from Mendari. A referral Mendari
  refuses as invalid input (BLOCK, a reason over 5,000 characters for one) gets a `warning`
  card saying it was not checked (no urgency or completeness check) and why, each reason naming
  where in the input and which rule, never the text; it is in the audit trail, not in the
  review queue, and the EHR's own referral workflow carries it. The trial agent is silent for a
  patient who is clearly not eligible, and the prior authorization service for an order it has
  no requirement set for (`not_checked`), unless the notes may document a red flag: then a
  `critical` card says so, and says nothing of the payer's requirements. An ABSTAIN because
  part of the record could not be read always gets its `info` card, whatever the agent shows
  otherwise; a decision whose input is also refused is a BLOCK, and shows the card the agent
  shows for refused input, if any.
- The Patient Inbox Router's card (`mendari-message-received`, Mendari's own hook) (applied to the former example agents; see the archive branch) names the team in its
  summary, says whether the message was routed or needs a person to confirm the team, and is
  `info` for the front desk, a medical assistant and billing, `warning` for a nurse and a
  physician, and `critical` for a possible emergency. A message with no suggested team, whether
  the engine abstained or refused it as invalid input, says it stays in the shared inbox for
  manual triage, and the detail says which.
- The prior authorization service is a Da Vinci CRD service. Its discovery entry declares
  `davinci-crd.version` (`2.2`) and one option, `coverage-info`, and each of its cards says it
  is `coverage-info` in `source.topic`. A request with
  `"extension": {"davinci-crd.configuration": {"coverage-info": false}}` gets no cards and no
  system actions. Each order is still decided and recorded.

The links on a card need `MENDARI_PUBLIC_URL`:

| Link | When | Where it goes |
|---|---|---|
| The audit trail (`absolute`) | `MENDARI_PUBLIC_URL` is set | `<public URL>/?pack=<pack>#audit/<decision>`, the decision's record, which needs a Mendari sign-in with `read` in the organization. The engine's reasons move behind this link, but for a list read without a source your EHR does not offer (`<list> may be incomplete: …`, below), which stays in the detail, and for an input refused as invalid (an agent's `refused` card), whose reasons stay in its detail. |
| The agent (`source.url`) | `MENDARI_PUBLIC_URL` is set | `<public URL>/?pack=<pack>#card`, the agent's card. |
| The launch (`smart`) | also SMART is on (`MENDARI_SMART_CLIENT_ID`), and the request's `fhirServer` is registered in `MENDARI_SMART_EHRS` to the caller's own organization | `<public URL>/smart/launch?pack=<pack>`. The EHR adds `iss` and `launch`. |

Without `MENDARI_PUBLIC_URL`, a card has no links and the reasons stay in its detail, but for
the confidence thresholds' reason (such as `confidence 0.94 is below the auto threshold 0.95`):
its figures carry no interval, and the outcome's sentence says what it decided. The audit trail
keeps it. A
clinician without a Mendari account sees no reasons behind the link: give pilot clinicians
accounts, or open the agent from the EHR (the launch link).

## Card feedback

When the clinician acts on a card, the EHR may send CDS Hooks 2.0 feedback to
`POST /cds-services/{id}/feedback`, with a token for that URL (or the `cds-hooks` key):

```json
{
  "feedback": [
    {
      "card": "<the card's uuid>",
      "outcome": "accepted",
      "acceptedSuggestions": [{"id": "<its suggestion's uuid>"}],
      "outcomeTimestamp": "2026-10-18T12:00:00-04:00"
    },
    {
      "card": "<another card's uuid>",
      "outcome": "overridden",
      "overrideReason": {
        "reason": {
          "system": "https://mendari.co/fhir/CodeSystem/card-override-reasons",
          "code": "clinical-judgement"
        }
      },
      "outcomeTimestamp": "2026-10-18T12:01:00-04:00"
    }
  ]
}
```

- 1 to 50 cards in one request, each once. The answer is `{"recorded": <n>}`.
- `accepted` names the card's one suggestion. `overridden` names one of the card's reasons, or
  none. Anything the card did not offer, a field CDS Hooks 2.0 does not define (a Coding is
  `system`, `code` and `display`), or an `outcomeTimestamp` with no time in UTC is a `422`.
  `extension`, which CDS Hooks 2.0 reserves for implementations, is accepted on any object of
  the body and never kept.
- Each card must be a decision of that service, in the caller's organization, and, with sign-in
  on (or for a CDS client's token), one the same caller asked for. Otherwise the answer is one
  `404` for all of them, whichever it was.
- It is all or nothing: a refused request records nothing.
- Each card's feedback is chained on its decision as `card.feedback`: the service, the outcome,
  the suggestion taken, the reason's system and code, the time and who sent it. `userComment`
  is never kept.
- It counts against the decide rate limit, as `cds:<id>`, and not against the daily cap: it
  decides nothing.

## Try it with the sandbox's client

Before an EHR does, the sandbox plays one: a CDS client that signs its own tokens, calling over
HTTP, on synthetic patients shaped as an EHR's FHIR API returns them.

**The vendor-shaped EHR profile.** `mendari sandbox seed --pack ehr-profile --fhir <FHIR base>`
loads it into a FHIR server, each resource under its own id (`ehr-trial-1`, …), so seeding again
replaces it. It holds five synthetic patients for each former example agent with a standard hook: the
Clinical Trial Prescreener (`patient-view`), the Prior Authorization Preflight and the Referral
Router (`order-sign`). Its shapes follow Epic's and Oracle Health's public FHIR documentation,
and it holds none of their data:

- a note is a DocumentReference that links to a Binary (`attachment.url`), as plain text, HTML or
  RTF;
- a Condition carries the site's own diagnosis code (a fictional system) and ICD-10-CM, the
  site's coding first for some patients and second for others;
- a medication is a MedicationRequest naming a Medication coded in RxNorm, with its dates in a
  dosage `boundsPeriod`;
- `Coverage.payor` refers to an Organization that holds the site's identifier for the payer;
- a referral's category carries the site's order type beside the referral code;
- no SNOMED CT anywhere.

It carries neither the sandbox tag nor a synthetic case id, as an EHR's data would not, so the
sandbox's worker, SMART launch and Bulk check never read it.

**The client.** `mendari sandbox cds-client --engine <URL> --fhir <FHIR base>`:

1. makes an EC P-384 key in memory, which it never writes anywhere, and serves its JWK Set at
   `<iss>/jwks.json` (`--listen`, default `127.0.0.1:8300`; `--iss`, the URL the engine reaches
   it at, default `http://` and the listening address);
2. prints its `MENDARI_CDS_CLIENTS` entry, with its keys by `jwks_uri` (`--id`, default
   `sandbox-client`; `--org`, default `default`);
3. checks that the FHIR server holds the profile's patients;
4. reads discovery, then calls each standard hook of each agent the profile has patients for,
   once per patient. Each call carries a new ES384 token: `iss` its own URL, `aud` the engine's
   public URL (`--audience`, default `--engine`) and the path, five minutes, a new `jti`, and
   `jku` in the header. The request sends no prefetch and names the FHIR server as
   `fhirServer`, so the engine reads the record there; an order hook's `draftOrders` are the
   patient's orders, as drafts.

It prints what each call answered, and exits 1 on any answer other than `200`, a hook answer
that is not CDS Hooks' (a `cards` list, each with a summary, an indicator and a source), or a
card saying part of the record could not be read for anything but a patient's RTF note
(`notes: unsupported_type`, below). Every hook answers `200`, so a read of the FHIR server that
failed (`could not read conditions: http_status`, say) shows only there. To run
it against your own engine, register the entry it prints, put its origin and the FHIR server's
on `MENDARI_FHIR_ALLOWED_SERVERS`, and give the engine's `MENDARI_PUBLIC_URL` as `--audience`.
Each run makes a new key, and an engine fetches a client's keys again at most once a minute, so
a second run within the minute is refused (`cds_key`).

In the compose sandbox, the profile `signin`'s service `cds-client` seeds the profile and runs
the client against both engines, the open one (under `MENDARI_CDS_SANDBOX=1`) and the signed-in
one (in organization `pilot`). Both register it at `http://cds-client:8300`. CI's `sandbox-e2e`
runs it on every change:

```bash
docker compose --profile signin run --rm --use-aliases cds-client
```

**What the agents cannot read in these shapes yet.** Since LP-23k the three agents are on spec
0.3 and read most of this data. A note that is only a link is read from the FHIR server (its
Binary, as text/plain or text/html); an RTF note is not, so the trial and prior-authorization
agents abstain for the patient whose note is RTF (`could not read notes: unsupported_type`, an
`info` card). A Condition is read by its ICD-10-CM coding and a referral by its FR101 coding
wherever they sit, so every referral is found. The trial agent reads the MedicationRequests and
the Medication each names (its RxNorm code, which the case's own record does not carry, is read
in addition). The prior-authorization agent follows the payer's reference to its Organization,
but the Organization holds only the site's own payer id, which the agent does not know and no
site code map translates (a map translates codings only): the payer reads as `other`, and the order
as `not_checked`. The owner confirmed this reading on 2026-11-07, and decided that a possible
red flag under a site's payer id is to show a card at `order-sign`, while other `not_checked`
orders stay quiet: built in the prior-authorization agent's 0.3.1, so such an order shows no card
unless its notes may document a red flag (that order shows the red-flag card, and reaches the
review queue at urgent priority). `tests/test_ehr_profile.py` pins each of these, with why: it is
a regression fixture, not a rate.

## Site codes

An EHR often codes things its own way: a local code system, or another code of a standard one.
An agent at spec 0.3 names sets of its own codes (`code_sets`) and reads a field through one
(`code_set`, [the spec](../spec/decision-pack-0.3.md#25-code-sets-03)). `MENDARI_SITE_CODES`
says how a site's codes translate into an agent's, one organization at a time. Off until you set
it: unset, no code is translated and every decision is made as before.

It is inline JSON, or the path of a JSON file. For example (every code here is fictional):

```json
{
  "default": {
    "version": "2026-10-19.1",
    "sets": {
      "referral_category": [
        {"system": "https://ehr.example.org/fhir/CodeSystem/order-types", "code": "REF-7",
         "as": "FR101"}
      ]
    }
  }
}
```

| Field | What it is |
|---|---|
| `<org>` | The organization whose decisions read with this map: one your people sign in to, `default` with sign-in off (and then the only one). At most 100. |
| `version` | Your own label for this version of the map, 1 to 64 visible ASCII characters. Every decision records it. |
| `sets` | By the name of a code set an agent declares, the translations into it. |
| `system`, `code` | The site's code: a code system URI (`http`, `https` or `urn:`) and a code in it. |
| `as` | The agent's code it stands for: one of the codes the set declares. |

At most 10,000 translations in one organization's map, and a site code once in a set. It is read
at startup, so a change takes effect at the next restart or redeploy. It is checked against the
code sets of the agents found on the server, served or not: a set no agent declares, or an `as`
that is not a code of its set, stops the server. The message names the setting, the
organization, the code set and the translation's position, for example
`MENDARI_SITE_CODES organization 'default', code set 'referral_category', translation 1: it
translates into a code the set does not declare`. It never shows a system or a code. A
translation applies in an agent whose set of that name lists its `as`, and only for that set's
fields. It only fills in where the agent's own codes give nothing: a coding in the set's own
system wins over a translated one wherever the field finds it, among one path's codings or on
a later one of its `paths`. A path that gives a code only through a translation passes to the
next, and the first translated code counts only when no path gives one of the set's own. So a
record whose own codes give a field its value is read the same with the map as without it.

What a decision does with it:

- A FHIR decision the server makes (`fhir/decide`, CDS Hooks, `$decide`, an EHR launch) reads
  its record with the map of the organization it acts for, never another's. Its record keeps
  `site_codes`: the map's `version` and `sha256`, the SHA-256 of the map's canonical form
  (`mendari.site-codes/1`: the organization, the version and each set's translations sorted by
  system and code). The same map written in another order has the same digest; any change, or
  one organization's map given to another, has another. Without a map it is `null`, as it is on
  a JSON decision. `/api/health` shows your organization's.
- The command line reads no map. `mendari decide --fhir` (with `--db` too) and `mendari bulk
  dataset` read every record as without one: a site's own codes are unknown, the decision
  records `site_codes: null`, and `MENDARI_SITE_CODES` is not read or checked. A dataset built
  from your bulk export is so not read as the server reads your records.
- Its data-coverage record is read with the same translations, so it says what the input read.
  It holds no translation, and a coding a translation matched counts by its system.
- What a [linked read](#linked-data) fetched (the Medication behind a `medicationReference`, say)
  is read with the same map, as the rest of the record is. A map translates codings only: a
  payer's identifier (`Organization.identifier`) is read as it is, never through a map.
- **A decision whose input took a value through a translation never automates.** AUTO becomes
  `HUMAN_REVIEW`, with the reason `site code translated (<code set>): a person decides`. A
  agent rule's BLOCK, which is final at once, becomes `HUMAN_REVIEW` too, with the reason `site
  code translated (<code set>): a person decides instead of its rule's BLOCK`. A decision its
  policy already sends to a person (`HUMAN_REVIEW`, or `ABSTAIN`: a confidence below the review
  threshold, no answer) keeps its outcome, with the first reason added; one that abstains
  because part of the record could not be read asks no provider, and its reasons name only
  what was not read. Nothing in an agent or a setting lifts this, a governance approval included.
  It stays until the site's map is validated (launch gate G5-6, `site-validation`), and
  lifting it then is the owner's decision.

A map is configuration, not part of an agent: no agent file or digest changes. A site's own SNOMED
CT codes may go in its map under its own licence; Mendari ships none. The Referral Router
declares three code sets (since LP-23k): `referral_category` (FR101, which `focus_where` reads:
translate the code your EHR marks a referral with into it, and the router finds your referrals),
`checklist_documents` and `checklist_reports` (the LOINC document and report codes its checklist
looks for). The trial and prior-authorization agents read their codes by code system, through no
code set, so a map does not reach them.

## What your EHR does not offer

An agent may read one list from several resource types: the trial agent reads its medications from
MedicationStatement and MedicationRequest. Some EHRs do not offer one of them at all. Epic offers
no R4 MedicationStatement API (only DSTU2 and STU3), so its read is refused (`unauthorized`), and,
as for any part of the record that cannot be read, the decision abstains: `could not read
medications: unauthorized`. That is the safe default. A drug the EHR never gave is not evidence
that the patient does not take it.

`MENDARI_EHR_DECLARATIONS` lets the site say what its EHR does not offer, one FHIR base and
organization at a time. Off until you set it. It is inline JSON, or the path of a JSON file:

```json
{
  "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4": {
    "org": "default",
    "version": "2026-11-08",
    "attested_by": "<who checked it>",
    "reference": "<where that check is recorded>",
    "not_offered": ["MedicationStatement"]
  }
}
```

- **Checked at startup.** The base must be on `MENDARI_FHIR_ALLOWED_SERVERS` and registered for
  launches (`MENDARI_SMART_EHRS`) or in a CDS client's `fhir_servers`, for the same organization
  (`default` with sign-in off). Each declared type must be one that an agent found here reads as
  one of several sources of a list, beside a source not declared. Declaring `DocumentReference`,
  `Patient`, or every source of a list, stops the server. So does anything else wrong; the
  refusal names the setting, the base and the field.
- **What it changes.** For a SMART launch from that base, or a CDS hook whose `fhirServer` is
  that base, acting for that organization, a prefetch key of a declared type is not read for a
  agent that may read its list without it, when another source of that list is read. A CDS client
  that sends the key with entries has them used; sent null, empty or as a search with no
  entries, the key is not offered. Nothing changes for `fhir/decide`, `$decide`, the command
  line, another base, another organization, or an agent that reads the type anywhere else.
- **What the decision records.** Its `ehr_declaration` (version, SHA-256, who attested it, its
  reference); in its data-coverage record the source and the list marked `not_offered` (its 0 is
  "not read", never "read and empty") and the key under `prefetch`. `/api/health` shows your
  organization's declarations.
- **It never automates.** AUTO, and an agent rule's BLOCK, become `HUMAN_REVIEW`, with the reason
  `<list> may be incomplete: read without <Type>, which this EHR does not offer (declared,
  <attested_by>): a person decides`, whatever the outcome (an input refused by the agent's input
  schema, or under `enforce` by US Core, is a final BLOCK, and its reason ends at the closing
  parenthesis). A card the agent shows keeps it in its detail even when its other reasons are
  behind the audit link (on an unreadable ABSTAIN's card and a refused input's, apart from what
  was not read or why it was refused). The trial agent shows none for `not_eligible`, which an
  incomplete list can give: such a decision reaches only the review queue (owner question O6).

Declaring MedicationStatement says more than that Epic lacks the API: it says every medication
the agent should see is in Epic's MedicationRequests, so "no insulin among the orders" may read as
"off insulin". Check, on your Epic, before you set it:

- whether patient-reported or historical home medications are returned as MedicationRequest,
  and with which `intent`. A trial agent that leaves out requests whose `intent` is `proposal`,
  `plan` or `option` (the trial agent since 0.4.0, #287) would not see home medications recorded so;
- whether the default MedicationRequest search (no `status`, no `category`) returns stopped,
  completed and inpatient orders: `no_insulin` looks back 90 days at recently stopped insulin;
- whether `Medication.code.text` (or the request's `display`) carries the generic name. The
  trial criteria match medications by name, so a brand-only name reads as no insulin, or no
  metformin.

Setting it on a hosted deployment is a clinical claim as well as a configuration change: the
person who attests it is named on every decision it applies to.

## Conformance runs

Mendari's own repository runs Inferno's Da Vinci CRD test kit (v0.15.0, suite
`crd_server_v221`) as an EHR's CDS client against a signed-in engine, weekly and by hand
(`conformance.yml`; how to run it yourself, and every known gap with its reason, in
[`sandbox/inferno/README.md`](https://github.com/mendari-hq/mendari/tree/main/sandbox/inferno)).
It is registered as any EHR is: the entry below, in that sandbox only. The kit's signing keys
are in its public repository, so never register it on a deployment.

```json
[
  {
    "id": "inferno-crd",
    "title": "Inferno CRD test kit (sandbox only)",
    "iss": "http://nginx/custom/crd_server_v221",
    "org": "pilot",
    "fhir_servers": ["http://nginx/custom/crd_server_v221/fhir"],
    "jwks_uri": "http://nginx/custom/crd_server_v221/jwks.json"
  }
]
```

Each hook call carries a new ES384 token with exactly these header fields (`alg`, `kid`, `typ`,
`jku`), and its `aud` is the engine's `MENDARI_PUBLIC_URL` plus the path called. The run proves,
on synthetic data, that discovery and the prior-authorization agent's `order-sign` answer a
signed CRD client, read the record from the EHR's FHIR server with the token the hook carries,
and abstain, saying what they could not read, when part of it cannot be read: with the EHR's
FHIR server failing every read (the kit's Technical Issues group), the answer's coverage is
`indeterminate` for a `technical` reason. With no Coverage to name, no coverage information is
sent at all. It does not prove your
deployment: TLS (the kit's test 1.01), your EHR's keys and their rotation, and your EHR's own
requests are yours to check (`Check it`, above). The same weekly run has Inferno's SMART App
Launch client suite against an EHR launch and the Bulk Data client suite against `mendari bulk
export`, and `terminology.yml` runs the HL7 validator over Mendari's synthetic FHIR resources
with a terminology server (tx.fhir.org). For an EHR that launches Mendari, `mendari smart check
<its FHIR base>` also compares the scopes a launch asks for with the EHR's `scopes_supported`
([Sign-in and roles](../deploying-with-phi.md#2-sign-in-and-roles)).

## Before a pilot

What Mendari's own tests and runs show, on synthetic data and test keys only, and what they leave
to a pilot:

- **End to end.** An EHR's signed token, a hook over HTTP whose record is read from the EHR's
  FHIR server, its card, the clinician's feedback on that card, then `mendari audit verify`
  passing over the trail, and failing once one of its events is changed, on SQLite and Postgres
  ([`tests/test_ehr_pilot_e2e.py`](https://github.com/mendari-hq/mendari/blob/main/tests/test_ehr_pilot_e2e.py)).
- **Not yet shown on a vendor's own EHR.** That every agent with a standard hook decides on the
  vendor's sandbox patients without a failed hook, and that `mendari smart check` passes against
  the vendor, is the pilot's first run (the named vendor is Epic, with Oracle Health as the
  backup). Until then the path is proven with test keys, the sandbox's client and Inferno only.
- **Notes the agents may not read.** The former example agents that read notes (the trial and
  prior-authorization agents) are on spec 0.3 since LP-23k, and read a note that is only a link
  from the FHIR server, but as text only when it is text/plain or text/html: an RTF, PDF or CDA
  note abstains (`unsupported_type`). So do notes longer than the agent's input allows in all (20,000
  characters, `too_large`; the text is never cut) and more notes than a decision reads (50,
  `cap`), which a patient with many notes may reach. On an EHR that sends notes that way, or keeps
  many notes for each patient, the agents that read notes may abstain for most patients with one,
  each decision going to a person with an `info` card. Read the decisions' data-coverage records
  (the audit view's "What it read") in the first days.
- **After the first run.** The first SMART launch on Epic's sandbox (2026-11-08) launched,
  authorized and read the patient; the trial agent abstained on MedicationStatement's refusal, on
  one link budget its notes had used, and on RTF twins counted twice. Each decision's "What it
  read" now says which prefetch reads failed and with which HTTP status, how many links it read
  for each budget, and the formats of the notes it could not read: read those lines after the
  first run, and before declaring what your EHR does not offer
  ([above](#what-your-ehr-does-not-offer)).
- **The validation mode.** Under `MENDARI_FHIR_VALIDATION=warn`, the default, input that is R4
  JSON but breaks R4's other rules is decided with its issues recorded; `enforce` blocks it
  before any provider runs ([Settings at a glance](../deploying-with-phi.md#8-settings-at-a-glance)).
- **What stays a risk** with all of this in place: trust in the EHR's key and its rotation, a
  compromised `jku` host, the decision id the EHR keeps, a misconfigured site map, a wrong
  declaration of what the EHR does not offer, and frequent
  abstention on records whose notes Mendari cannot read
  ([threat model](../security/threat-model.md#residual-risks-and-assumptions)).
