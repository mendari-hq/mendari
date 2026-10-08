# Using the API

Mendari's REST and FHIR API by example: a quickstart in three calls, FHIR in and out, guarding
a model's replies, and authentication. A running server shows the same examples on its own landing page, `/docs`, with
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
`POST /cds-services/{id}` decides on a hook's context and prefetch, answering with cards.

**SMART on FHIR.** When a deployment sets `MENDARI_SMART_CLIENT_ID`, an EHR launches Mendari at
`/smart/launch`, and the dashboard opens on that patient's work.

## Guarding a model's replies

A pack with a `guard` section, such as the AI Guard reference pack, checks a model's draft before
a patient reads it. `POST /api/packs/{id}/guard` answers `{status, reply, outcome, recommendation,
reasons, fallback, decision_id}`: the draft is `released` only when the decision is automated with
a release recommendation; otherwise it is `held_for_review` or `blocked`. Never send a blocked
draft: act on `fallback`. Here a dose above the daily maximum is blocked:

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
who can reach the server can call every operation, and the `Authorization` header is ignored.

**With sign-in on:**

- People sign in at `/auth/login` with their organization's identity provider; the dashboard and
  the API reference use that session.
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

What each call needs (public, `read`, `decide`, an administrator, …) comes from the server's route
rules; a running server's `/docs` lists every call with it. Setting up sign-in, roles and keys is
in [Deploying with real patient data](../deploying-with-phi.md).
