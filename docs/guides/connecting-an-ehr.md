# Connecting an EHR

An EHR calls Mendari's CDS Hooks services (`GET /cds-services` and
`POST /cds-services/{id}`) in one of two ways:

- **With a Mendari key.** An administrator makes a key from the `cds-hooks` preset and the
  EHR sends it as `Authorization: Bearer ohk_…`
  ([the presets](../deploying-with-phi.md#2-sign-in-and-roles)). This needs sign-in on.
- **With its own signed token**, as CDS Hooks 2.0 describes ("Trusting CDS Clients"). You
  register the EHR once, and each call carries a short-lived JWT the EHR signs with its own
  private key. Mendari never holds that key. This page is about this way.

Off until you register a client: with `MENDARI_CDS_CLIENTS` unset, a JWT on `/cds-services` is
refused in every mode, open mode included, and everything else works as before. The decision behind it is
[ADR-0014](../decisions/ADR-0014-a-cds-client-is-a-principal.md).

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
| `iss` | yes | Exactly what the EHR's tokens carry as `iss`. Some EHRs send their FHIR base; Inferno's CRD kit sends its own URL. |
| `org` | yes | The organization its calls act for, and whose decisions they become. With sign-in off it must be `default`. In memberships mode it needs a record. |
| `fhir_servers` | yes | The FHIR bases (1 to 20) the EHR may send as `fhirServer`. Each must have its origin on `MENDARI_FHIR_ALLOWED_SERVERS`, and if `MENDARI_SMART_EHRS` registers the same base for launches, it must be to the same organization. |
| `jwks` or `jwks_uri` | one of the two | The EHR's public keys. `jwks` is a JWK Set by value. `jwks_uri` is the one URL Mendari fetches them from: https (plain http only for a loopback or single-label host), with its origin on `MENDARI_FHIR_ALLOWED_SERVERS`. |
| `sub` | no | When set, every token must carry exactly this `sub`. |
| `tenant` | no | When set, every token must carry exactly this `tenant`. When not set, a token with any `tenant` is refused: an EHR shared by several organizations is not supported yet. |
| `iss_is_fhir_server` | no | `true`: the `fhirServer` a call sends must also be its `iss` (which must then be one of `fhir_servers`). |
| `origins` | no | The browser origins the EHR calls from. Not used yet: CORS comes in a later step. |

Also set `MENDARI_PUBLIC_URL` to the address the EHR calls. A token's `aud` is that URL plus the
path called, so without it the server does not start.

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
list the services and call them, and nothing else:

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

## In open mode

With sign-in off, a JWT on `/cds-services` is verified too, and without a registry it is
refused. A request with no credential is answered as before, so a local demo that sends no
token still works. Off `/cds-services` open mode reads no credential, a JWT included. A tool that always sends its own JWT, such as the public CDS Hooks Sandbox,
has to be registered first, with `org` set to `default`.

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
`cds_fhir_server_not_registered`.

## Not built yet

These come in the next LP-23 steps:

- Open mode answering CDS Hooks only on this machine, and CORS for the registered `origins`.
- A hook that always answers: a record Mendari cannot read gives a recorded ABSTAIN with its
  reason, and every FHIR decision says what was read.
- Cards and card feedback as CDS Hooks 2.0 and Da Vinci CRD expect.
- Inferno's CRD kit calling Mendari as the CDS client in CI.
