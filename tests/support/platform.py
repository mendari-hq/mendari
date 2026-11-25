# SPDX-License-Identifier: Apache-2.0
"""What the platform and server tests decide with: the triage fixture as a server serves it.

Synthetic test fixture. Not clinically reviewed. Not for patient care.

`tests/fixtures/agents/triage` (`mendari-test/triage`, `TRIAGE_DIR`) routes a
synthetic patient message to the team that reads it first. On its default providers (the
keyword rules in both slots) nothing is automated (its policy's `automation_requires`); a test
that needs AUTO with sign-in on runs `calm_agent` (tests/conftest.py). This module gives the
tests:

- `TRIAGE` and `TRIAGE_API`: its id, and its routes' prefix;
- messages that reach each of its outcomes on its default providers (`URGENT`, `REFILL`, ...);
- `COMMUNICATION`, `PATIENT` and `BUNDLE`: a synthetic message as an EHR sends it (its FHIR
  mapping takes a Communication in);
- `bundled_agents`: the fixtures' agents a test server serves, read from their manifests, so no
  test counts them or names one it does not decide with;
- `local_site`: a local site's datasets for the server's `datasets_dir`;
- `attested`: a copy of the fixture with what a maintained agent commits about itself (its
  benchmark and leaderboard), which the fixture itself does not;
- `SmartEhr`: an EHR that launches Mendari (SMART on FHIR), whose FHIR server answers from the
  chart-summary fixture's synthetic showcase record, with `smart_launch`, `full_smart_launch`,
  `smart_tenant` and `smart_routed`, as `tests/test_smart.py` launches.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from mendari import Decision, Pack, load_pack
from mendari.core.registry import SearchPath, discover
from mendari.evals import evaluate, leaderboard, resolve_dataset
from tests.conftest import CLINICAL_DIR, FIXTURE_AGENTS, TRIAGE_DIR
from tests.support.fixture_records import SHOWCASE, Case

TRIAGE = "mendari-test/triage"
TRIAGE_API = f"/api/agents/{TRIAGE}"
CLINICAL = "mendari-test/clinical-review"
CHART = "mendari-test/chart-summary"  # a patient's FHIR record in: what an EHR launch decides

# Messages, each with what the fixture decides on its default providers (the keyword rules).
# HUMAN_REVIEW, a nurse at priority stat (rule `possible-emergency`):
URGENT = {"message": {"body": "I have had chest pain since last night."}}
# HUMAN_REVIEW, the pharmacy at priority routine (AUTO with the screen's agent):
REFILL = {"message": {"body": "Please renew my prescription for metformin."}}
# HUMAN_REVIEW, the front desk (AUTO with the screen's agent):
APPOINTMENT = {"message": {"body": "Can I move my appointment to Friday?"}}
# HUMAN_REVIEW, billing (never AUTO: `allow_auto: false`):
BILL = {"message": {"body": "Why was I charged twice this month?"}}
# ABSTAIN, no suggestion:
UNPLACED = {"message": {"body": "I am not sure who to ask about my visit last week."}}
# BLOCK (rule `abusive-message`):
ABUSIVE = {"message": {"body": "You people are useless."}}
# BLOCK: the input schema refuses it.
BLANK = {"message": {"body": ""}}

# A synthetic message as an EHR sends it: the triage fixture's FHIR mapping reads the
# Communication (its topic, its text and its channel) and not the Patient beside it. Its text is
# a possible emergency (HUMAN_REVIEW, a nurse at priority stat: a critical card).
COMMUNICATION: dict[str, Any] = {
    "resourceType": "Communication",
    "id": "msg-1",
    "status": "in-progress",
    "subject": {"reference": "Patient/pat-1"},
    "topic": {"text": "Help"},
    "medium": [{"coding": [{"system": "x", "code": "sms"}]}],
    "sent": "2026-09-25T10:00:00Z",
    "payload": [
        {"contentString": "I have had chest pain since last night"},
        {"contentString": "and it is getting worse."},
    ],
}
PATIENT: dict[str, Any] = {"resourceType": "Patient", "id": "pat-1", "birthDate": "1950-03-02"}
BUNDLE: dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "collection",
    "entry": [{"resource": COMMUNICATION}, {"resource": PATIENT}],
}


def bundled_agents() -> list[Pack]:
    """The agents a test server serves as bundled (the fixtures' folder, `FIXTURE_AGENTS`), by
    id, read from their manifests: whichever are there."""
    found = discover([SearchPath(FIXTURE_AGENTS, "maintained")])
    return sorted(found.packs, key=lambda agent: agent.id)


def local_site(directory: Path) -> Path:
    """`directory` holding a local site's datasets, as a server's `datasets_dir` reads them:
    `local-site-example` labels the triage fixture's questions (its same-day symptoms, most worded
    as a patient might, which its keyword rules route worse than its benchmark set), and
    `clinical-local-site-example` the clinical-review fixture's, so it is offered to that agent
    only."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, source in (
        ("local-site-example", TRIAGE_DIR / "data" / "critical.jsonl"),
        ("clinical-local-site-example", CLINICAL_DIR / "data" / "test.jsonl"),
    ):
        records = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        lines = [json.dumps({**record, "id": f"local-{i:05d}"}) for i, record in enumerate(records)]
        (directory / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory


def attested(root: Path) -> Pack:
    """The copy of the triage fixture at `root` (`agent_copy`, its manifest naming
    `evals/benchmark.json` as its benchmark: `names_its_benchmark`), with what a maintained agent
    commits about itself: its benchmark, an evaluation of its default providers on its benchmark
    set, and a leaderboard of that and of its screen run by its canned agent."""
    agent = load_pack(root)
    dataset = resolve_dataset(agent, agent.manifest.evaluation.benchmark_dataset or "test")
    report = evaluate(Decision(agent), dataset)
    screened = evaluate(Decision(agent, providers={"screen": "agent"}, origin_cap=False), dataset)
    (root / "evals").mkdir(exist_ok=True)
    (root / "evals" / "benchmark.json").write_text(json.dumps(report), encoding="utf-8")
    board = leaderboard.build(agent, [report, screened])
    (root / leaderboard.PATH).write_text(json.dumps(board), encoding="utf-8")
    return load_pack(root)


def names_its_benchmark(manifest: dict[str, Any]) -> None:
    """An `agent_copy` manifest edit: the copy names `evals/benchmark.json` as its benchmark."""
    manifest["evaluation"]["benchmark"] = "evals/benchmark.json"


# -- an EHR that launches Mendari (SMART on FHIR) --------------------------------------------------

SMART_EHR = "https://ehr.example.org"
SMART_ISS = f"{SMART_EHR}/fhir"
SMART_APP = "https://mendari.test"  # Mendari's public URL, where the EHR sends the browser back
SMART_CLIENT = "mendari-app"  # Mendari's client id at the EHR (MENDARI_SMART_CLIENT_ID)


class SmartEhr:
    """An EHR's authorization server and FHIR server in one `httpx.MockTransport` handler: the
    FHIR base `iss`, the authorization server and its keys under `root`. It checks PKCE and signs
    RS256 id tokens naming a practitioner (`clinician-1`, `Practitioner/pr-1`; `claims` overrides
    it, a `None` taking a claim out), and answers FHIR reads and searches (each search every
    resource of its type) from one synthetic patient's record: the chart-summary fixture's
    showcase record (`fixture_records.SHOWCASE`) unless given another `case`."""

    def __init__(self, iss: str = SMART_ISS, root: str = SMART_EHR, case: Case = SHOWCASE):
        self.iss = iss
        self.root = root
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes: dict[str, dict[str, Any]] = {}
        self.tokens: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.claims: dict[str, Any] = {}  # overrides for the next id_token
        self.patient = case.id
        self.token_prefix = "access"  # the access tokens it issues: <prefix>-<n>
        self.resources = case.resources()

    def authorize(self, url: str) -> str:
        """The browser at the authorization endpoint: approve and return the code."""
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert params["response_type"] == "code"
        assert params["aud"].lower().rstrip("/") == self.iss.lower()  # the base it sent
        assert params["code_challenge_method"] == "S256"
        code = f"code-{len(self.codes)}"
        self.codes[code] = params
        return code

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == f"{self.iss}/.well-known/smart-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.iss,
                    "jwks_uri": f"{self.root}/keys",
                    "authorization_endpoint": f"{self.root}/auth/authorize",
                    "token_endpoint": f"{self.root}/auth/token",
                    "code_challenge_methods_supported": ["S256"],
                    "scopes_supported": [
                        "openid", "fhirUser", "launch", "launch/patient", "patient/*.rs",
                        "user/*.rs",
                    ],
                },
            )  # fmt: skip
        if url == f"{self.root}/keys":
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": "k1", "alg": "RS256"}]})
        if url == f"{self.root}/auth/token":
            return self.token(dict(parse_qs(request.content.decode())))
        if url.startswith(self.iss):
            return self.fhir(request)
        return httpx.Response(404, json={})

    def token(self, form: dict[str, list[str]]) -> httpx.Response:
        code = self.codes.pop(form["code"][0], None)
        if code is None:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "no"})
        verifier = form["code_verifier"][0].encode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=")
        assert challenge.decode() == code["code_challenge"], "PKCE verifier does not match"
        assert form["redirect_uri"][0] == code["redirect_uri"] == f"{SMART_APP}/smart/callback"
        assert form["client_id"][0] == SMART_CLIENT
        access = f"{self.token_prefix}-{len(self.tokens)}"
        self.tokens.add(access)
        now = int(time.time())
        claims = {"iss": self.iss, "aud": SMART_CLIENT, "exp": now + 300, "iat": now}
        claims |= {"nonce": code["nonce"], "sub": "clinician-1", "fhirUser": "Practitioner/pr-1"}
        claims = {k: v for k, v in {**claims, **self.claims}.items() if v is not None}
        id_token = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})
        body = {"access_token": access, "token_type": "Bearer", "expires_in": 3600}
        body |= {"scope": code["scope"], "patient": self.patient, "encounter": "enc-1"}
        return httpx.Response(200, json={**body, "id_token": id_token})

    def fhir(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization", "").removeprefix("Bearer ") not in self.tokens:
            return httpx.Response(401, json={"resourceType": "OperationOutcome"})
        path = request.url.path.removeprefix(f"{urlsplit(self.iss).path}/")
        headers = {"content-type": "application/fhir+json"}
        if "/" in path:
            kind, rid = path.split("/", 1)
            found = [r for r in self.resources if r["resourceType"] == kind and r["id"] == rid]
            return httpx.Response(
                200 if found else 404, json=found[0] if found else {}, headers=headers
            )
        entries = [{"resource": r} for r in self.resources if r["resourceType"] == path]
        return httpx.Response(
            200, json={"resourceType": "Bundle", "entry": entries}, headers=headers
        )


def smart_launch(app: TestClient, agent: str = CHART, iss: str = SMART_ISS) -> Any:
    """The EHR opens Mendari's launch URL for `agent`, from the FHIR base `iss`."""
    return app.get(
        "/smart/launch", params={"iss": iss, "launch": "xyz", "pack": agent}, follow_redirects=False
    )


def full_smart_launch(
    app: TestClient, ehr: SmartEhr, iss: str | None = None, agent: str = CHART
) -> Any:
    """The EHR launches Mendari (from `iss`, the EHR's own base unless given) and the browser
    comes back with the code: the callback's answer."""
    started = smart_launch(app, agent, iss=iss or ehr.iss)
    assert started.status_code == 302, started.text
    code = ehr.authorize(started.headers["location"])
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    return app.get("/smart/callback", params={"code": code, "state": state}, follow_redirects=False)


def smart_tenant(iss: str) -> SmartEhr:
    """One tenant of an EHR host: its FHIR base `iss`, its authorization server above it."""
    return SmartEhr(iss=iss, root=iss.removesuffix("/fhir"))


def smart_routed(*ehrs: SmartEhr) -> httpx.MockTransport:
    """One transport for several EHRs, each answering under its own root."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for ehr in ehrs:
            if url.startswith(f"{ehr.root}/"):
                return ehr(request)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)
