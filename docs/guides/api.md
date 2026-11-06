# Using the API

Mendari's REST and FHIR API by example: a quickstart in three calls, FHIR in and out, CDS
Hooks, guarding a model's replies, and authentication. A running server shows the same examples on its own landing page, `/docs`, with
its address filled in and a table of what every call needs; its full reference, with "Try it
out", is `/docs/reference`.

Every example below is one of the scripts in `src/mendari/dashboard/snippets/`, included here as
it is. CI runs them against a local server, once in open mode and once with a `service` key
(`scripts/check_doc_snippets.py`), so they work as written; the Python example runs as a
program. They need `curl` and `jq`, and every body is synthetic.

## Before you start

In a clone, `uv sync` installs Mendari with the server (without uv: `pip install '.[server]'`;
the `server` extra brings uvicorn, which `mendari serve` needs). The server serves the reference
packs; to serve your own too, set `MENDARI_PACKS_PATH` to their folders (`:`-separated, searched
first). `MENDARI_PACKS` (comma-separated ids) then narrows what is served to those packs, which
must be on the path.

Start a server, and in another terminal name it and, with sign-in on, your API key:

<!-- check: skip long-running: starts a server; check_doc_snippets.py starts the same app in CI -->
```bash
uv run mendari serve                      # http://localhost:8000
```

<!-- check: skip settings: sets the two variables check_doc_snippets.py sets for each pass -->
```bash
export MENDARI=http://localhost:8000      # the server
export MENDARI_KEY=                       # with sign-in on: an API key (ohk_…); open mode ignores it
```

Each script runs as a whole (`bash quickstart.sh`), or step by step: copy the lines under each
`# step:` comment.

## Quickstart in three calls

List the packs, decide on a synthetic patient message (one of the pack's behaviour tests), and read
the decision's audit record:

<!-- check: include quickstart.sh -->
```bash
--8<-- "quickstart.sh"
```

Every decision ends as `AUTO` (safe to act on), `HUMAN_REVIEW` (a person decides, with the pack's
recommendation), `ABSTAIN` (not confident enough to say) or `BLOCK` (the input or a safety rule
stops it). Check `outcome`, not only the status code: an input that fails the pack's schema is a
recorded decision with `outcome: BLOCK`.

## FHIR in, FHIR out

The FHIR base is `/fhir`, and `GET /fhir/metadata` is its CapabilityStatement. `POST
/fhir/$decide` takes `Parameters` (`pack`, and `data` as a Bundle) and returns the decision as a
FHIR Bundle, a `GuidanceResponse` first: act on its `outcome`, not its `status`. Decisions are read
and searched as `Task`, and their audit trail as `AuditEvent` and `Provenance`, your
organization's only. Mendari is a decision engine, not a FHIR data server: it stores and serves no
clinical resources.

<!-- check: include fhir.sh -->
```bash
--8<-- "fhir.sh"
```

**CDS Hooks.** `GET /cds-services` lists the services, one per pack that declares a hook, and
`POST /cds-services/{id}` decides on a hook's context and prefetch, answering with cards. An
EHR calls them with a Mendari key (the `cds-hooks` preset), or, once registered, with its own
signed token: [Connecting an EHR](connecting-an-ehr.md).

`POST /cds-services/{id}` decides on a hook's context and prefetch, answering with cards.
A prefetch key you leave out, or send empty (`{}`, `[]`, `""`), is read from your `fhirServer`,
if the deployment allows it; with no `fhirServer` the request is a `412`. A null value means you
have no data, and it is not read. Every value you send goes through the structural R4 check: a
structure error is a `400`. A hook always answers: when a key cannot be read (a failed read, an
OperationOutcome, a value that is not a resource, a resource or search page that is not R4
JSON), or a note is only a link or does not decode, the decision is `ABSTAIN` with reasons such
as `could not read labs: timeout`, it is recorded, and its card is an `info` card that says so.
Reasons never repeat a URL, a query or what a server answered. A request fault (the wrong hook, a
malformed field, a required resource you sent something else for) is still a `4xx`.

**Cards and feedback.** A card's `uuid` is its decision's id. Its one suggestion has a `uuid` of
its own, and the card says `selectionBehavior: at-most-one` and offers three `overrideReasons`
(`https://mendari.co/fhir/CodeSystem/card-override-reasons`: not relevant, already addressed,
clinical judgement, each with its display). The summary uses the pack's display names, and the
detail says the pack's status, the notice and one plain sentence for the outcome. When the
deployment sets `MENDARI_PUBLIC_URL`, the reasons are behind the card's link to the decision in
the audit trail (which needs a Mendari sign-in), `source.url` is the pack's card, and, when SMART
is on and your `fhirServer` is registered for launches in your organization, a `smart` link
opens the pack. Without it, the reasons are in the detail, but for the confidence thresholds',
whose figures carry no interval. The prior authorization service is a
Da Vinci CRD service: discovery declares `davinci-crd.version` and the `coverage-info` option,
and a request whose `extension.davinci-crd.configuration` sets `coverage-info` to `false` gets no
cards and no system actions, while each order is still decided and recorded.

`POST /cds-services/{id}/feedback` takes CDS Hooks 2.0 feedback, 1 to 50 cards: each `accepted`
with the suggestion taken (`acceptedSuggestions`), or `overridden` with one of the card's
reasons or none, and its `outcomeTimestamp`. Each is chained on the decision as `card.feedback`,
with codes and ids only: `userComment` is never kept. It is all or nothing. A suggestion or a
reason the card did not offer is a `422`, and so is a field CDS Hooks 2.0 does not define or an
`outcomeTimestamp` with no time in UTC. `extension`, which CDS Hooks 2.0 reserves for
implementations, is accepted on any object and never kept. A card that is not a decision of that
service in your organization, or, with sign-in on, not one your own credential asked for, is one
`404` for all of them. It needs the decide scope and counts against the decide rate limit, not
the daily cap.

<!-- check: skip placeholder: the card's uuid is one a hook gave your EHR -->
```bash
curl -s -X POST "$MENDARI/cds-services/mendari.referral-router/feedback" \
  -H "authorization: Bearer $MENDARI_KEY" -H 'content-type: application/json' \
  -d '{"feedback": [{"card": "<card uuid>", "outcome": "overridden",
       "overrideReason": {"reason": {"system": "https://mendari.co/fhir/CodeSystem/card-override-reasons",
                                     "code": "already-addressed"}},
       "outcomeTimestamp": "2026-10-18T12:00:00-04:00"}]}'
# {"recorded": 1}
```

List the services and call the inbox's hook, with a synthetic patient and message as prefetch.
With sign-in off, the CDS services answer on this machine only (`127.0.0.1`, `localhost`): from
anywhere else they are a `403`, whatever the request carries.

<!-- check: include cds.sh -->
```bash
--8<-- "cds.sh"
```

**What a decision read.** Every decision made from FHIR (CDS Hooks, `fhir/decide`, `$decide`, the
SMART launch) carries `data_coverage` in its record, which `GET /api/audit/{id}` returns: for
each mapped field and list, the paths it looked for, the code systems the pack declares, the
pack's own codes, and what it found, as counts, with what could not be read counted by reason.
It holds no value, id or text. The answers of `fhir/decide` and the SMART launch's decide hold it
too. A CDS Hooks card and `$decide`'s GuidanceResponse do not: they give the decision id (the
card's `uuid` and `extension.mendari.decision_id`, the GuidanceResponse `identifier`). In the
dashboard, a FHIR decision's record shows it as "What it read": counts by field and by reason.
A decision on JSON input carries `null`.

**SMART on FHIR.** When a deployment sets `MENDARI_SMART_CLIENT_ID`, an EHR launches Mendari at
`/smart/launch`, and the dashboard opens on that patient's work.

## Guarding a model's replies

A pack with a `guard` section, such as the AI Guard reference pack, checks a model's draft before
a patient reads it. `POST /api/packs/{id}/guard` answers `{status, reply, outcome, recommendation,
reasons, fallback, decision_id}` and the whole `decision`: the draft is `released` only when the
decision is automated with a release recommendation; otherwise it is `held_for_review` or
`blocked`. Never send a blocked draft: act on `fallback`. Here a dose above the daily maximum is blocked:

<!-- check: include guard.sh -->
```bash
--8<-- "guard.sh"
```

The same check in Python, without a server:

<!-- check: include guard.py -->
```python
--8<-- "guard.py"
```

## Authentication

**Open mode** (the default, without `MENDARI_OIDC_ISSUER` and `MENDARI_OIDC_CLIENT_ID`): anyone
who can reach the server can call every operation, and the `Authorization` header is ignored,
but for a JWT on `/cds-services`: that is a CDS client's token, verified as with sign-in on, and
refused unless an EHR is registered for it.

**With sign-in on:**

- People sign in at `/auth/login` with their organization's identity provider; the dashboard and
  the API reference use that session. A script that reads with a person's session cookie sends
  `X-Mendari-Request: 1` too, as Mendari's own pages do: a read with the cookie and neither
  `Sec-Fetch-Site` nor `Origin` is otherwise refused, as another site's link in an older
  browser would be.
- Machines send `Authorization: Bearer ohk_…`, an API key a signed-in administrator creates with
  `POST /api/auth/keys` and rotates with `POST /api/auth/keys/{id}/rotate` (the old key works
  for an overlap, 24 hours unless asked). A key has one role, `viewer` or `service`, expires (90
  days unless asked, 365 at most), and may carry scopes that narrow it: SMART v2 reads such as `system/Task.rs`, and
  `https://mendari.co/fhir/scope/decide` or `https://mendari.co/fhir/scope/outcome`. The decide
  scope also lists the packs it may decide with (`GET /api/packs`, `GET /cds-services`); no
  scope reaches Mendari's other reads under `/api`. For each documented integration path there
  is a preset (`"preset": "fhir"`, `"cds-hooks"`, …) with the role and scope it needs:
  [the list](../deploying-with-phi.md#2-sign-in-and-roles). The dashboard's Access view (API
  keys) makes keys from these presets, shows each once, and rotates and revokes them.
- An EHR registered in `MENDARI_CDS_CLIENTS` calls the CDS Hooks services with its own signed
  token (RS384 or ES384, `aud` the URL it calls, a `jti` used once), as the principal
  `cds:<id>`, which reaches nothing else: [Connecting an EHR](connecting-an-ehr.md).
- With SMART Backend Services on, a registered client exchanges a signed assertion (RS384 or
  ES384, never a client secret) for a five-minute access token, `Bearer mat_…`. A client keeps
  its id when its public keys change (`POST /api/auth/clients/{id}/keys`, or the Access view's
  Clients tab): its tokens stop at once.
- A credential never goes in a URL or a form. Refusals are a `401` or `403` with
  `WWW-Authenticate`, except a signed-in person's who has not yet acknowledged the notice
  (`GET /api/notice`): a `403` with `Mendari-Notice-Required` on routes that take data in.

<!-- check: include auth.sh -->
```bash
--8<-- "auth.sh"
```

An organization's administrators read the chained events of what it owns (its keys, clients,
people, automation approvals and holds, evaluation reports) at `GET /api/audit/administrative`,
oldest first, or newest first with `order=desc`, and paged (`next` is the following page's
`after`, in the same order; `kind`, `subject` such as `key:<id>`, and `since` / `until` as
instants with their offset narrow it), and export them as
FHIR AuditEvents of ids and codes at `GET /api/audit/administrative/export/fhir`. A decision's
own trail is `GET /api/audit/{id}`.

What each call needs (public, `read`, `decide`, an administrator, …) comes from the server's route
rules; a running server's `/docs` lists every call with it. Setting up sign-in, roles and keys is
in [Deploying with real patient data](../deploying-with-phi.md).
