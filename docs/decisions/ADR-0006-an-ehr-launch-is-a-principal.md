# ADR-0006: An EHR launch is a narrow principal, and its session row is sealed

## Status
Accepted (2026-10-03)

## Context
A clinician can open Mendari from the EHR with a SMART on FHIR launch: the EHR signs them in,
gives Mendari an access token and the patient in context, and Mendari decides about that patient.
The launch kept its own session, separate from Mendari's sign-in, and its routes were public in
the route rules. The Phase 4b review found what that means once sign-in is on:

- **C5.** `/api/smart/decide` made decisions with no Mendari principal: outside roles, scopes and
  the organization filter, into the default organization, attributed to nobody. A signed-in
  viewer with a launch cookie could decide. An `Authorization` header sent with a launch cookie
  was never looked at, though everywhere else a bearer credential decides alone.
- **S5.** The EHR access token, the patient and the PKCE verifier sat readable in the session
  table, under every PHI mode, and stayed there after the session expired.

The SMART launch is off in production. It has to be fixed before any site turns it on with
sign-in. The first site with PHI runs Mendari on its own servers, from an EHR not yet chosen.

## Decision
- **The EHR's sign-in identifies the clinician.** A launch's verified id_token (issuer and
  subject) makes a principal of kind `ehr`, in the organization registered for that EHR's FHIR
  base: `MENDARI_SMART_EHRS="<FHIR base>=<org>:<view|decide>,..."`. With sign-in on, a launch
  from an unregistered base, without `openid fhirUser`, or by a user who is not a Practitioner or
  PractitionerRole (a patient portal) is refused. The registration is read on every request, so
  removing it ends that EHR's launches at once. Open mode is unchanged.
- **A launch may view, or view and decide, for the launched patient. Nothing else.** Two new
  permissions, held only by `ehr` principals: reading the patient and the focus resources through
  the EHR, and deciding. They hold no Mendari role. The organization's reviewers use the audit
  trail. The dashboard shows a launch only the patient view, with the decision inline, pinned
  to the launch that opened the window: a window whose launch was replaced says so and shows no
  patient (one launch per browser at a time).
- **A bearer credential decides alone everywhere**, `/api/smart/*` included. Signing out of
  Mendari ends the browser's launch too.
- **The chain names who asked.** Every new `decision.created` names the requester when sign-in is
  on: a person by verified email, a key or a client by id, an EHR user by email or issuer and
  subject. The FHIR exports render it as the requestor.
- **The launch's rows are sealed.** The session and pending-launch rows are encrypted with
  AES-256-GCM. The key is derived (HKDF) from the row's own cookie, so the database alone opens
  nothing, and no new secret has to be provisioned or rotated. The associated data binds each
  sealed value to its row and kind. A row that will not open, or is in plaintext, ends the launch.
  `offline_access` is refused: no refresh token is ever stored.
- **No refresh, no iframe for now.** A launch ends with the EHR's token: about hourly on Epic,
  every 9.5 minutes on Oracle Health. The clinician opens Mendari again from the EHR. Mendari
  runs in a new window or the EHR's embedded window (Oracle's WebView2 control, Epic's embedded
  window); inside an iframe the launch still fails safely. Both wait for the pilot EHR.

## Consequences
- A launch is attributable, limited per principal, and contained in one organization.
- An EHR whose sign-in a site does not trust cannot be registered for it.
- A launch in flight at the upgrade has to be started again once.
- Rolling back past this change breaks only `/api/smart/*`, until the launch rows are deleted.

## Alternatives Considered
- **A Mendari sign-in as well, bound to the launch.** Rejected: two sign-ins, and no iframe ever.
- **A Mendari role per registered EHR.** Rejected: a stolen launch would reach the whole review
  queue, inputs included.
- **A new key for the session rows.** Rejected: the cookie already is a secret the database
  never holds; a separate key adds provisioning and rotation for no gain against a database leak.
- **Sealing sign-in session rows too.** Deferred: they hold no token, and sealing them would sign
  everyone out.
