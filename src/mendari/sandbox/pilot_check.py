# SPDX-License-Identifier: Apache-2.0
"""The v0.6 pilot, scripted: an organization runs a portal-message agent with its own identity
provider, PHI-safe settings, monitoring and governance approvals (the roadmap's exit check). The
agent is the bundled demo agent, unless another message agent is named.

It runs against an engine configured for sign-in, PHI mode `encrypt` and an input hash key
(`MENDARI_INPUT_HASH_KEY`: every decision's input hash is keyed), and an OpenID Connect
provider with a scriptable sign-in form (the CI job uses navikt's mock-oauth2-server). Every
person signs in through the real flow: the engine's `/auth/login`, the provider's form, the
engine's callback. Each person who mints a key, decides or reviews first acknowledges the
research-preview notice (`/api/notice/acknowledge`), as the dashboard asks once per version.
Then:

1. an admin creates an API key for the message worker (role `service`);
2. the worker decides synthetic portal messages with the key;
3. a reviewer reads the queue (inputs decrypted, each read chained) and resolves a review, which
   is recorded under the reviewer's verified email. The decision holds what the agent wrote
   (the demo agent drafts an acknowledgement with every message, ADR-0027): the reviewer is
   shown each text and gives it a verdict, without which the review is refused;
4. a viewer can read a decision but not its input, nor a text it holds, and can't resolve a
   review;
5. someone on the AI committee (role `governance`) approves automation of a risk level 2 or 3
   agent, and its next decision carries the approval. The demo agent is level 0 and needs no
   approval, so this step runs when such an agent is named (`approve`), and is reported as
   left out otherwise;
6. monitoring counts the week's decisions, and the audit chain verifies.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from mendari.core.registry import resolve_agent
from mendari.errors import MendariError
from mendari.evals.behaviour_tests import load_cases
from mendari.sandbox import DEMO_AGENT
from mendari.sandbox.fhir_workflow import labelled_cases
from mendari.server.origin import OWN_REQUEST

COOKIE = "mendari_session"
SIGNIN = "mendari_signin"  # ties the engine's /auth/login to its /auth/callback
EMERGENCY = {"message": {"body": "Crushing chest pain and my left arm is numb"}}
NOT_APPROVED = (
    "no risk level 2 or 3 agent was named (--approve), so the committee's approval was left out"
)
# Who signs in, and the groups the identity provider gives them (the engine maps groups to roles
# with MENDARI_OIDC_ROLE_MAP; see the CI job `pilot-check`). The chain names each by their
# verified email (`email()`).
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
        """The browser's part of the OpenID Connect flow; returns the headers each call sends:
        the session cookie, and Mendari's own request header, without which a read with the
        cookie and no Fetch Metadata is refused as another site's link (server/origin.py)."""
        name, groups = PEOPLE[who]
        started = self.http.get(f"{self.engine}/auth/login")
        authorize = _redirect(started, "the engine's /auth/login")
        # The engine's cookies are Secure, and the pilot may run on plain http, so this script
        # carries them by hand: first the one that ties the sign-in to this "browser".
        binding = _cookie(started, SIGNIN, "sign-in")
        claims = {"name": name, "groups": groups, "email": email(who), "email_verified": True}
        signed = self.http.post(authorize, data={"username": who, "claims": json.dumps(claims)})
        callback = _redirect(signed, "the identity provider's sign-in form")
        if not callback.startswith(f"{self.engine}/auth/callback"):
            raise MendariError(f"the identity provider sent the browser to {callback[:80]}")
        done = self.http.get(callback, headers={"cookie": binding})
        _redirect(done, "the engine's /auth/callback")
        return {"cookie": _cookie(done, COOKIE, "session"), OWN_REQUEST: "1"}

    def call(self, method: str, path: str, auth: dict[str, str], **kwargs: Any) -> httpx.Response:
        return self.http.request(method, f"{self.engine}{path}", headers=auth, **kwargs)

    def expect(self, response: httpx.Response, status: int, what: str, *, log: bool = True) -> Any:
        if response.status_code != status:
            raise MendariError(f"{what}: HTTP {response.status_code}, expected {status}")
        if log:
            self.steps.append(what)
        return response.json() if response.content else None


def acknowledge(pilot: Pilot, person: dict[str, str], who: str) -> None:
    """A person acknowledges the research-preview notice (`mendari.notices`): until then the
    engine refuses their minting, deciding and reviewing (`MENDARI_NOTICE_ACKNOWLEDGEMENT`)."""
    pilot.expect(
        pilot.call("POST", "/api/notice/acknowledge", person),
        200,
        f"{who} acknowledges the research-preview notice",
    )


def review_of(item: Mapping[str, Any], label: str) -> dict[str, Any]:
    """The review the pilot's reviewer sends for a decision in the queue: its label and, for
    each text the decision holds (ADR-0027), a verdict: released with a label they confirm,
    withheld with one they correct. A reviewer gives a verdict on what they were shown: a held
    text the queue did not open for them stops the pilot (the failure names the output, never
    its text)."""
    review: dict[str, Any] = {"final_label": label, "reviewer": "someone else"}
    held = [text for text in item.get("outputs") or () if text.get("state") == "held"]
    for text in held:
        if text.get("text_status") != "open" or not text.get("text"):
            raise MendariError(f"the reviewer can't read {text.get('name')!r} in the queue")
    if held:
        verdict = "release" if item.get("recommendation") == label else "withhold"
        review["texts"] = {text["name"]: verdict for text in held}
    return review


def run(
    *,
    engine: str,
    messages: int = 12,
    agent: str = DEMO_AGENT,
    approve: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """The pilot on `agent` (a message agent: an id or a directory, the engine serving it),
    deciding `messages` of its benchmark cases. With `approve` (a risk level 2 or 3 agent the
    engine serves), the committee approves its automation and its next decision carries the
    approval; without, that step is left out and the result says so."""
    loaded = resolve_agent(agent)
    governed = None if approve is None else resolve_agent(approve)
    primary = loaded.manifest.questions[loaded.manifest.policy.primary]
    with httpx.Client(follow_redirects=False, timeout=60, transport=transport) as http:
        pilot = Pilot(engine, http)
        health = pilot.expect(pilot.call("GET", "/api/health", {}), 200, "health")
        if not health.get("sign_in"):
            raise MendariError("the engine must run with sign-in on and PHI mode encrypt")

        admin = pilot.sign_in("admin")
        me = pilot.expect(pilot.call("GET", "/api/auth/me", admin), 200, "the admin signs in")
        if me["principal"]["roles"] != ["admin"]:
            raise MendariError(f"the admin's roles are {me['principal']['roles']}")
        # The settings are shown to a signed-in reader only; anyone gets status and version.
        settings = pilot.expect(pilot.call("GET", "/api/health", admin), 200, "settings")
        if settings.get("phi_mode") != "encrypt":
            raise MendariError("the engine must run with sign-in on and PHI mode encrypt")
        acknowledge(pilot, admin, "the admin")
        body = {"name": "message worker", "role": "service"}
        key = pilot.expect(
            pilot.call("POST", "/api/auth/keys", admin, json=body), 201, "an API key is created"
        )
        worker = {"authorization": f"Bearer {key['key']}"}

        cases = labelled_cases(loaded).records[: max(0, messages - 1)]
        inputs = [EMERGENCY] + [record["input"] for record in cases]
        decided = [
            pilot.expect(
                pilot.call("POST", f"/api/agents/{loaded.id}/decide", worker, json=item),
                200,
                "the worker decides a message",
                log=False,
            )
            for item in inputs
        ]
        pilot.steps.append(f"the worker decides {len(decided)} messages with its API key")
        require_keyed_hashes(decided)

        reviewer = pilot.sign_in("reviewer")
        acknowledge(pilot, reviewer, "the reviewer")
        queue = pilot.expect(
            pilot.call("GET", f"/api/review?agent_id={loaded.id}", reviewer),
            200,
            "the reviewer opens the queue",
        )
        if not queue or queue[0]["record"]["input"] is None:
            raise MendariError("the reviewer can't see the inputs in the queue")
        first = queue[0]
        # The reviewer's label is one of the question's own: the recommendation, or its first.
        label = first["recommendation"] or next(iter(primary.criteria))
        review = review_of(first, label)
        resolved = pilot.expect(
            pilot.call("POST", f"/api/review/{first['id']}", reviewer, json=review),
            200,
            "the reviewer resolves a review"
            + (", with a verdict on each text the agent wrote" if "texts" in review else ""),
        )

        viewer = pilot.sign_in("viewer")
        seen = pilot.expect(
            pilot.call("GET", f"/api/audit/{first['id']}", viewer),
            200,
            "the viewer reads a decision",
        )
        if seen["record"]["input"] is not None or not seen.get("input_restricted"):
            raise MendariError("the viewer could read an input")
        if any("text" in text for text in seen.get("outputs") or ()):
            raise MendariError("the viewer could read a text the agent wrote")
        if seen["reviewer"] != email("reviewer"):
            raise MendariError(f"the review is recorded for {seen['reviewer']!r}")
        pilot.expect(
            pilot.call(
                "POST",
                f"/api/review/{decided[1]['id']}",
                viewer,
                json={"final_label": label, "reviewer": "x"},
            ),
            403,
            "the viewer may not resolve a review",
        )

        approval = None
        if governed is None:
            pilot.steps.append(NOT_APPROVED)
        else:
            chair = pilot.sign_in("governance")
            level = int(governed.manifest.risk.level)
            asked = {
                "agent_id": governed.id,
                "reference": "AI committee minutes, pilot",
                "days": 90,
            }
            approval = pilot.expect(
                pilot.call("POST", "/api/governance", chair, json=asked),
                201,
                f"the committee approves automation of a level {level} agent",
            )
            case = load_cases(governed)[0]["input"]
            approved = pilot.expect(
                pilot.call("POST", f"/api/agents/{governed.id}/decide", worker, json=case),
                200,
                "the worker decides with the approved agent",
            )
            if (approved.get("governance") or {}).get("approved_by") != email("governance"):
                raise MendariError("the decision does not carry the committee's approval")

        monitoring = pilot.expect(
            pilot.call("GET", f"/api/monitoring?agent_id={loaded.id}", admin),
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
        if not reads or reads[0]["payload"]["reader"] != email("reviewer"):
            raise MendariError("the reviewer's read of the input is not in the chain")

    return {
        "steps": pilot.steps,
        "decided": len(decided),
        "reviewed": {"id": resolved["id"], "final_label": label},
        "approval": approval
        and {"agent_id": approval["agent_id"], "approver": approval["approver"]},
        "monitoring_last_7_days": week,
        "input_reads_logged": len(reads),
    }


def email(who: str) -> str:
    """The verified email the identity provider gives `who`, which the chain names them by."""
    return f"{who}@pilot.example.org"


def _cookie(response: httpx.Response, base: str, what: str) -> str:
    """A `cookie` header with the engine's cookie `base` from `response`: `__Host-<base>`, or
    the plain name on a local or dotless host."""
    for name in (f"__Host-{base}", base):
        value = response.cookies.get(name)
        if value:
            return f"{name}={value}"
    raise MendariError(f"the engine set no {what} cookie")


def require_keyed_hashes(decided: list[dict[str, Any]]) -> None:
    """Every decision's input hash is the keyed HMAC (`hmac-sha256:<key id>:<hex>`): a plain
    SHA-256 of a short input can be found by guessing."""
    if not all(str(item.get("input_hash")).startswith("hmac-sha256:") for item in decided):
        raise MendariError("the input hashes are not keyed: set MENDARI_INPUT_HASH_KEY")


def _redirect(response: httpx.Response, what: str) -> str:
    if response.status_code not in (302, 303, 307):
        raise MendariError(f"{what} answered HTTP {response.status_code}, not a redirect")
    return str(response.headers["location"])
