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
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from mendari.errors import MendariError
from mendari.sandbox.fhir_records import SANDBOX_TAG

COOKIE = "mendari_smart"
_FOCUS = {
    "mendari/patient-inbox-router": "Communication",
    "mendari/referral-router": "ServiceRequest",
}
_TAG = f"{SANDBOX_TAG['system']}|{SANDBOX_TAG['code']}"


def launch_value(patient: str, provider: str) -> str:
    """smart-launcher-v2's EHR launch parameters (provider-ehr, no encounter, skip login and
    consent, public client, PKCE when offered), base64url-encoded as the launcher expects."""
    fields = [0, patient, provider, "NONE", 1, 1, 0, "", "", "", "", "", "", "", 0, 1, ""]
    return base64.urlsafe_b64encode(json.dumps(fields).encode()).decode().rstrip("=")


def find_patient(fhir: str, pack_id: str) -> str:
    """A synthetic patient who has one of the pack's focus resources."""
    kind = _FOCUS.get(pack_id)
    if kind is None:
        raise MendariError(f"the sandbox launches {sorted(_FOCUS)}, not {pack_id}")
    response = httpx.get(
        f"{fhir.rstrip('/')}/{kind}",
        params={"_tag": _TAG, "_count": "1"},
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
) -> dict[str, Any]:
    """Launch, decide up to `limit` of the patient's work items, log out; return what happened."""
    engine = engine.rstrip("/")
    patient = patient or find_patient(fhir, pack_id)
    iss = f"{launcher.rstrip('/')}/v/r4/fhir"
    with httpx.Client(follow_redirects=False, timeout=60) as browser:
        started = browser.get(
            f"{engine}/smart/launch",
            params={"iss": iss, "launch": launch_value(patient, provider), "pack": pack_id},
        )
        authorize = _redirect(started, "the engine's launch")
        approved = _redirect(browser.get(authorize), "the launcher's authorize endpoint")
        if not approved.startswith(f"{engine}/smart/callback"):
            raise MendariError(f"the launcher sent the browser to {approved.split('?')[0]}")
        done = browser.get(approved)
        _redirect(done, "the engine's callback")
        # The session cookie is Secure; the sandbox runs on plain http, so send it by hand.
        sid = done.cookies.get(COOKIE)
        if not sid:
            raise MendariError("the engine set no session cookie")
        cookie = {"cookie": f"{COOKIE}={sid}"}
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
    return {
        "patient": session["patient"],
        "fhir_user": session.get("fhir_user"),
        "work_items": len(items),
        "decided": decided,
        "session_ended": not ended["active"],
    }


def _redirect(response: httpx.Response, step: str) -> str:
    if response.status_code not in (302, 303, 307):
        raise MendariError(f"{step} answered HTTP {response.status_code}: {response.text[:200]}")
    return str(response.headers["location"])


def _json(response: httpx.Response) -> Any:
    if not response.is_success:
        raise MendariError(f"{response.request.url.path} answered HTTP {response.status_code}")
    return response.json()
