"""The v0.6 pilot, scripted: an organization runs the inbox router with its own identity provider,
PHI-safe settings, monitoring and governance approvals (the roadmap's exit check).

It runs against an engine configured for sign-in, PHI mode `encrypt` and an input hash key
(`MENDARI_INPUT_HASH_KEY`: every decision's input hash is keyed), and an OpenID Connect
provider with a scriptable sign-in form (the CI job uses navikt's mock-oauth2-server). Every
person signs in through the real flow: the engine's `/auth/login`, the provider's form, the
engine's callback. Then:

1. an admin creates an API key for the message worker (role `service`);
2. the worker decides synthetic inbox messages with the key;
3. a reviewer reads the queue (inputs decrypted, each read chained) and resolves a review, which
   is recorded under the reviewer's name;
4. a viewer can read a decision but not its input, and can't resolve a review;
5. someone on the AI committee (role `governance`) approves automation of a level 2 pack, and the
   next decision carries the approval;
6. monitoring counts the week's decisions, and the audit chain verifies.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from mendari.core.registry import resolve_pack
from mendari.errors import MendariError
from mendari.evals.pack_tests import load_cases
from mendari.sandbox import synthetic_inbox

COOKIE = "mendari_session"
INBOX = "mendari/patient-inbox-router"
TRIAL = "mendari/clinical-trial-prescreener"  # risk level 2
EMERGENCY = {
    "message": {"body": "Crushing chest pain and my left arm is numb", "channel": "portal"}
}
# Who signs in, and the groups the identity provider gives them (the engine maps groups to roles
# with MENDARI_OIDC_ROLE_MAP; see the CI job `pilot-check`).
PEOPLE = {
    "admin": ("Pilot Admin", ["informatics"]),
    "reviewer": ("Nurse Pilot", ["clinicians"]),
    "viewer": ("Clerk Pilot", ["front-desk"]),
    "governance": ("Committee Chair", ["ai-committee"]),
}


class Pilot:
    def __init__(self, engine: str, http: httpx.Client):
        self.engine = engine.rstrip("/")
        self.http = http
        self.steps: list[str] = []

    def sign_in(self, who: str) -> dict[str, str]:
        """The browser's part of the OpenID Connect flow; returns the session cookie header."""
        name, groups = PEOPLE[who]
        started = self.http.get(f"{self.engine}/auth/login")
        authorize = _redirect(started, "the engine's /auth/login")
        claims = {"name": name, "groups": groups}
        signed = self.http.post(authorize, data={"username": who, "claims": json.dumps(claims)})
        callback = _redirect(signed, "the identity provider's sign-in form")
        if not callback.startswith(f"{self.engine}/auth/callback"):
            raise MendariError(f"the identity provider sent the browser to {callback[:80]}")
        done = self.http.get(callback)
        _redirect(done, "the engine's /auth/callback")
        # The session cookie is Secure; the pilot may run on plain http, so send it by hand.
        sid = done.cookies.get(COOKIE)
        if not sid:
            raise MendariError("the engine set no session cookie")
        return {"cookie": f"{COOKIE}={sid}"}

    def call(self, method: str, path: str, auth: dict[str, str], **kwargs: Any) -> httpx.Response:
        return self.http.request(method, f"{self.engine}{path}", headers=auth, **kwargs)

    def expect(self, response: httpx.Response, status: int, what: str, *, log: bool = True) -> Any:
        if response.status_code != status:
            raise MendariError(f"{what}: HTTP {response.status_code}, expected {status}")
        if log:
            self.steps.append(what)
        return response.json() if response.content else None


def run(
    *, engine: str, messages: int = 12, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    with httpx.Client(follow_redirects=False, timeout=60, transport=transport) as http:
        pilot = Pilot(engine, http)
        health = pilot.expect(pilot.call("GET", "/api/health", {}), 200, "health")
        if not health.get("sign_in") or health.get("phi_mode") != "encrypt":
            raise MendariError("the engine must run with sign-in on and PHI mode encrypt")

        admin = pilot.sign_in("admin")
        me = pilot.expect(pilot.call("GET", "/api/auth/me", admin), 200, "the admin signs in")
        if me["principal"]["roles"] != ["admin"]:
            raise MendariError(f"the admin's roles are {me['principal']['roles']}")
        body = {"name": "message worker", "role": "service"}
        key = pilot.expect(
            pilot.call("POST", "/api/auth/keys", admin, json=body), 201, "an API key is created"
        )
        worker = {"authorization": f"Bearer {key['key']}"}

        inputs = [EMERGENCY] + [
            r["input"] for r in synthetic_inbox.local_site(n=messages - 1, seed=606)
        ]
        decided = [
            pilot.expect(
                pilot.call("POST", f"/api/packs/{INBOX}/decide", worker, json=item),
                200,
                "the worker decides a message",
                log=False,
            )
            for item in inputs
        ]
        pilot.steps.append(f"the worker decides {len(decided)} messages with its API key")
        require_keyed_hashes(decided)

        reviewer = pilot.sign_in("reviewer")
        queue = pilot.expect(
            pilot.call("GET", f"/api/review?pack_id={INBOX}", reviewer),
            200,
            "the reviewer opens the queue",
        )
        if not queue or queue[0]["record"]["input"] is None:
            raise MendariError("the reviewer can't see the inputs in the queue")
        first = queue[0]
        label = first["recommendation"] or "nurse"
        resolved = pilot.expect(
            pilot.call(
                "POST",
                f"/api/review/{first['id']}",
                reviewer,
                json={"final_label": label, "reviewer": "someone else"},
            ),
            200,
            "the reviewer resolves a review",
        )

        viewer = pilot.sign_in("viewer")
        seen = pilot.expect(
            pilot.call("GET", f"/api/audit/{first['id']}", viewer),
            200,
            "the viewer reads a decision",
        )
        if seen["record"]["input"] is not None or not seen.get("input_restricted"):
            raise MendariError("the viewer could read an input")
        if seen["reviewer"] != PEOPLE["reviewer"][0]:
            raise MendariError(f"the review is recorded for {seen['reviewer']!r}")
        pilot.expect(
            pilot.call(
                "POST",
                f"/api/review/{decided[1]['id']}",
                viewer,
                json={"final_label": "nurse", "reviewer": "x"},
            ),
            403,
            "the viewer may not resolve a review",
        )

        chair = pilot.sign_in("governance")
        approval = pilot.expect(
            pilot.call(
                "POST",
                "/api/governance",
                chair,
                json={"pack_id": TRIAL, "reference": "AI committee minutes, pilot", "days": 90},
            ),
            201,
            "the committee approves automation of a level 2 pack",
        )
        case = load_cases(resolve_pack(TRIAL))[0]["input"]
        trial = pilot.expect(
            pilot.call("POST", f"/api/packs/{TRIAL}/decide", worker, json=case),
            200,
            "the worker decides with the approved pack",
        )
        if (trial.get("governance") or {}).get("approved_by") != PEOPLE["governance"][0]:
            raise MendariError("the decision does not carry the committee's approval")

        monitoring = pilot.expect(
            pilot.call("GET", f"/api/monitoring?pack_id={INBOX}", admin),
            200,
            "monitoring shows the week",
        )
        week = monitoring["last_7_days"]["decisions"]
        if week < len(decided):
            raise MendariError(f"monitoring counts {week} decisions, expected {len(decided)}+")
        chain = pilot.expect(
            pilot.call("GET", "/api/audit/verify", admin), 200, "the audit chain verifies"
        )
        if not chain.get("ok"):
            raise MendariError(f"the audit chain does not verify: {chain}")
        events = pilot.call("GET", f"/api/audit/{first['id']}", viewer).json()["events"]
        reads = [e for e in events if e["event"] == "input.read"]
        if not reads or reads[0]["payload"]["reader"] != PEOPLE["reviewer"][0]:
            raise MendariError("the reviewer's read of the input is not in the chain")

    return {
        "steps": pilot.steps,
        "decided": len(decided),
        "reviewed": {"id": resolved["id"], "final_label": label},
        "approval": {"pack_id": approval["pack_id"], "approver": approval["approver"]},
        "monitoring_last_7_days": week,
        "input_reads_logged": len(reads),
    }


def require_keyed_hashes(decided: list[dict[str, Any]]) -> None:
    """Every decision's input hash is the keyed HMAC (`hmac-sha256:<key id>:<hex>`): a plain
    SHA-256 of a short input can be found by guessing."""
    if not all(str(item.get("input_hash")).startswith("hmac-sha256:") for item in decided):
        raise MendariError("the input hashes are not keyed: set MENDARI_INPUT_HASH_KEY")


def _redirect(response: httpx.Response, what: str) -> str:
    if response.status_code not in (302, 303, 307):
        raise MendariError(f"{what} answered HTTP {response.status_code}, not a redirect")
    return str(response.headers["location"])
