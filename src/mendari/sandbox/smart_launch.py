# SPDX-License-Identifier: Apache-2.0
"""A scripted SMART on FHIR EHR launch against the sandbox (docker compose): the EHR and the
browser in one, over plain HTTP.

It picks a synthetic patient with something for the pack to decide, then does what an EHR and a
browser would:
1. open the engine's launch URL with `iss` (the launcher's FHIR base) and a `launch` value;
2. follow the redirect to the launcher's authorize endpoint;
3. follow the redirect back to the engine's callback.

The launcher's launch value asks it to skip login and consent. The script then uses the session
like the dashboard does: the patient's work items, a decision for each, and logging out. This is
the roadmap's exit check: "the inbox and referral packs run from a SMART launch against the HAPI
sandbox".

With sign-in on and the launcher registered (`MENDARI_SMART_EHRS`, the compose profile
`signin`), `expect_org` also reads the engine's own audit store (`db`, so the script runs next
to the engine) for who each decision's `decision.created` event says asked, as that
organization: a decision another organization owns reads as nobody (ADR-0006).

`from_launch_url` is the browser alone, for an EHR that makes its own launch URL (the engine's
`/smart/launch` with the EHR's `iss` and `launch`, as Inferno's SMART App Launch client suite
does, sandbox/inferno/README.md): the same three steps from that URL, then the patient in
context read through the EHR's FHIR server with the launch's token, and logging out.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx

from mendari.core import tenancy
from mendari.core.audit import AuditStore
from mendari.errors import MendariError
from mendari.sandbox.fhir_records import REFERRAL, SANDBOX_TAG
from mendari.server.origin import OWN_REQUEST

COOKIE = "mendari_smart"
LAUNCH = "mendari_launch"  # ties the engine's /smart/launch to its /smart/callback
_TAG = f"{SANDBOX_TAG['system']}|{SANDBOX_TAG['code']}"
# Each pack's focus resource, and the search for one of its synthetic cases'. A referral is found
# by its category: the prior-authorization pack's seeded orders are tagged ServiceRequests too
# (`mendari sandbox seed --pack mendari/prior-auth-preflight`), and none is a referral.
_FOCUS = {
    "mendari/patient-inbox-router": ("Communication", {}),
    "mendari/referral-router": (
        "ServiceRequest",
        {"category": f"{REFERRAL['system']}|{REFERRAL['code']}"},
    ),
}


def launch_value(patient: str, provider: str) -> str:
    """smart-launcher-v2's EHR launch parameters (provider-ehr, no encounter, skip login and
    consent, public client, PKCE when offered), base64url-encoded as the launcher expects."""
    fields = [0, patient, provider, "NONE", 1, 1, 0, "", "", "", "", "", "", "", 0, 1, ""]
    return base64.urlsafe_b64encode(json.dumps(fields).encode()).decode().rstrip("=")


def find_patient(fhir: str, pack_id: str) -> str:
    """A synthetic patient who has one of the pack's focus resources (for the referral router,
    a referral, whichever other packs' cases the server holds)."""
    focus = _FOCUS.get(pack_id)
    if focus is None:
        raise MendariError(f"the sandbox launches {sorted(_FOCUS)}, not {pack_id}")
    kind, narrowed = focus
    response = httpx.get(
        f"{fhir.rstrip('/')}/{kind}",
        params={"_tag": _TAG, **narrowed, "_count": "1"},
        headers={"accept": "application/fhir+json", "cache-control": "no-cache"},
        timeout=30,
    )
    response.raise_for_status()
    entries = response.json().get("entry") or []
    if not entries:
        raise MendariError(f"no synthetic {kind} in {fhir}: run `mendari sandbox seed` first")
    reference = entries[0]["resource"].get("subject", {}).get("reference", "")
    return str(reference).rsplit("/", 1)[-1]


def run(
    *,
    engine: str,
    launcher: str,
    fhir: str,
    pack_id: str,
    patient: str | None = None,
    provider: str = "sandbox-clinician",
    limit: int = 5,
    expect_org: str | None = None,
    db: str | None = None,
) -> dict[str, Any]:
    """Launch, decide up to `limit` of the patient's work items, log out; return what happened.
    With `expect_org`, also who each decision's chain says asked, read from `db` as that
    organization (`requested_by`)."""
    if expect_org is not None and db is None:
        raise MendariError("checking the organization needs the engine's database (db)")
    engine = engine.rstrip("/")
    patient = patient or find_patient(fhir, pack_id)
    iss = f"{launcher.rstrip('/')}/v/r4/fhir"
    with httpx.Client(follow_redirects=False, timeout=60) as browser:
        started = browser.get(
            f"{engine}/smart/launch",
            params={"iss": iss, "launch": launch_value(patient, provider), "pack": pack_id},
        )
        authorize = _redirect(started, "the engine's launch")
        # The engine's cookies are Secure and the sandbox runs on plain http, so this script
        # carries them by hand: first the one that ties the launch to this "browser".
        binding = _cookie(started, LAUNCH, "launch")
        approved = _redirect(browser.get(authorize), "the launcher's authorize endpoint")
        if not approved.startswith(f"{engine}/smart/callback"):
            raise MendariError(f"the launcher sent the browser to {approved.split('?')[0]}")
        done = browser.get(approved, headers={"cookie": binding})
        _redirect(done, "the engine's callback")
        # As the dashboard's requests: the launch cookie, and Mendari's own request header, which
        # a read with the cookie and no Fetch Metadata needs (server/origin.py).
        cookie = {"cookie": _cookie(done, COOKIE, "session"), OWN_REQUEST: "1"}
        session = _json(browser.get(f"{engine}/api/smart/session", headers=cookie))
        items = _json(
            browser.get(f"{engine}/api/smart/focus", params={"pack_id": pack_id}, headers=cookie)
        )
        decided = []
        for item in items[:limit]:
            body = {"pack_id": pack_id, "focus": item["ref"]}
            result = _json(browser.post(f"{engine}/api/smart/decide", json=body, headers=cookie))
            decision = result["decision"]
            decided.append(
                {
                    "focus": item["ref"],
                    "outcome": decision["outcome"],
                    "recommendation": decision["recommendation"],
                    "decision_id": decision["id"],
                    "input_issues": len(decision.get("input_issues", [])),
                }
            )
        browser.post(f"{engine}/api/smart/logout", headers=cookie).raise_for_status()
        ended = _json(browser.get(f"{engine}/api/smart/session", headers=cookie))
    result = {
        "patient": session["patient"],
        "fhir_user": session.get("fhir_user"),
        "work_items": len(items),
        "decided": decided,
        "session_ended": not ended["active"],
    }
    if expect_org is not None and db is not None:
        ids = [item["decision_id"] for item in decided]
        result |= {"org": expect_org, "requested_by": who_asked(db, expect_org, ids)}
    return result


def from_launch_url(url: str, *, engine: str) -> dict[str, Any]:
    """An EHR launch from the launch URL the EHR made (`url`: the engine's `/smart/launch` with
    the EHR's own `iss` and `launch`): follow it to the EHR's authorize endpoint and back to the
    engine's callback, read the patient in context (`/api/smart/patient`, which reads the EHR's
    FHIR server with the launch's token), and log out. Only a URL on the engine's own launch
    path is opened."""
    engine = engine.rstrip("/")
    if not url.startswith(f"{engine}/smart/launch?"):
        raise MendariError(f"the launch URL is not {engine}/smart/launch")
    with httpx.Client(follow_redirects=False, timeout=60) as browser:
        started = browser.get(url)
        authorize = _redirect(started, "the engine's launch")
        binding = _cookie(started, LAUNCH, "launch")
        approved = _redirect(browser.get(authorize), "the EHR's authorize endpoint")
        if not approved.startswith(f"{engine}/smart/callback"):
            raise MendariError(f"the EHR sent the browser to {approved.split('?')[0]}")
        done = browser.get(approved, headers={"cookie": binding})
        _redirect(done, "the engine's callback")
        cookie = {"cookie": _cookie(done, COOKIE, "session"), OWN_REQUEST: "1"}
        session = _json(browser.get(f"{engine}/api/smart/session", headers=cookie))
        patient = _json(browser.get(f"{engine}/api/smart/patient", headers=cookie))
        browser.post(f"{engine}/api/smart/logout", headers=cookie).raise_for_status()
        ended = _json(browser.get(f"{engine}/api/smart/session", headers=cookie))
    return {
        "patient": session["patient"],
        "scope": session.get("scope"),
        "patient_read": patient.get("id") == session["patient"],
        "session_ended": not ended["active"],
    }


def who_asked(db: str, org: str, decision_ids: list[str]) -> list[str | None]:
    """For each decision, who its `decision.created` event says asked (`requested_by`), read as
    `org`: None for a decision `org` does not own, or one that names nobody (open mode)."""
    named: list[str | None] = []
    with AuditStore(db) as store, tenancy.acting_for(org):
        for decision_id in decision_ids:
            item = store.item(decision_id)
            events = item["events"] if item is not None else []
            created = next((e for e in events if e["event"] == "decision.created"), None)
            named.append(created["payload"].get("requested_by") if created else None)
    return named


def names_launcher_user(actor: str | None, launcher: str) -> bool:
    """Whether the chain names the launch's user as the launcher's id_token does with no verified
    email: `<issuer>|<subject>`, the issuer on the launcher's own origin."""
    issuer = re.escape(launcher.rstrip("/"))
    return actor is not None and re.fullmatch(rf"{issuer}(/[^\s|]*)?\|\S+", actor) is not None


def _cookie(response: httpx.Response, base: str, what: str) -> str:
    """A `cookie` header with the engine's cookie `base` from `response`: `__Host-<base>`, or
    the plain name on a local or dotless host (the sandbox's `engine`)."""
    for name in (f"__Host-{base}", base):
        value = response.cookies.get(name)
        if value:
            return f"{name}={value}"
    raise MendariError(f"the engine set no {what} cookie")


def _redirect(response: httpx.Response, step: str) -> str:
    if response.status_code not in (302, 303, 307):
        raise MendariError(f"{step} answered HTTP {response.status_code}: {response.text[:200]}")
    return str(response.headers["location"])


def _json(response: httpx.Response) -> Any:
    if not response.is_success:
        raise MendariError(f"{response.request.url.path} answered HTTP {response.status_code}")
    return response.json()
