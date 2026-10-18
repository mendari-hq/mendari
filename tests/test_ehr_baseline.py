# SPDX-License-Identifier: Apache-2.0
"""What the EHR scorecard's level 2 rests on (notes/ehr-scorecard.md, LP-23a), where no earlier
test showed it. With sign-in on, the CDS Hooks services take only a credential Mendari issued,
and an EHR's own signed token is refused as one Mendari did not issue (CT-2a, CT-2b). And the
outside tools CI runs, pinned job by job: the HL7 FHIR validator, offline, over Mendari's outputs
and the sandbox's inputs (`fhir-conformance`, CE-2a), and the SMART app against smart-launcher-v2
(`sandbox-e2e`, CE-2b). The jobs' own runs are where the tools pass; these tests fail when a job
is removed, is allowed to fail, or stops running its tool."""

from __future__ import annotations

import fnmatch
import importlib.util
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient

from mendari import AuditStore
from mendari.server.app import create_app
from mendari.server.scopes import DECIDE
from tests.conftest import encrypt_mode, signed_in, without_request_id
from tests.test_auth import INVALID, REALM
from tests.test_limits import decisions_in, order_sign

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
HOST = "https://mendari.test"
HOOK = "/cds-services/mendari.referral-router"
EHR = "https://ehr.example.org"


@pytest.fixture
def served(db_target: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, str]]:
    """Sign-in on (the identity provider is never called), on SQLite or Postgres."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    with TestClient(create_app(db_path=db_target), base_url=HOST) as client:
        yield client, db_target


def person(db: str) -> dict[str, str]:
    with AuditStore(db) as store:
        return signed_in(store, role="admin")


def ehr_token(alg: str) -> str:
    """A CDS Hooks token as an EHR signs one (CDS Hooks 2.0, "Trusting CDS Clients"), with a key
    made for this test: Mendari has no registry of EHR keys yet (LP-23b)."""
    key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if alg == "RS384"
        else ec.generate_private_key(ec.SECP384R1())
    )
    now = int(time.time())
    claims = {
        "iss": EHR,
        "sub": "ehr-client-1",
        "aud": f"{HOST}{HOOK}",
        "iat": now,
        "exp": now + 300,
        "jti": str(uuid.uuid4()),
    }
    headers = {"kid": "ehr-1", "jku": f"{EHR}/jwks", "typ": "JWT"}
    return jwt.encode(claims, key, algorithm=alg, headers=headers)


def test_with_sign_in_on_discovery_and_a_hook_need_a_credential_mendari_issued(
    served: tuple[TestClient, str],
) -> None:
    """CT-2a. Without a credential discovery and the hook are a `401` and nothing is decided; a
    key made from the `cds-hooks` preset (role `service`, scoped to deciding) reads discovery
    and calls the hook."""
    client, db = served
    for refused in (client.get("/cds-services"), client.post(HOOK, json=order_sign(1))):
        assert refused.status_code == 401
        assert refused.headers["www-authenticate"] == REALM
    assert decisions_in(db) == 0
    made = client.post(
        "/api/auth/keys",
        json={"name": "an EHR", "role": "service", "preset": "cds-hooks"},
        headers=person(db),
    )
    assert made.status_code == 201, made.text
    assert made.json()["scope"] == DECIDE
    key = {"authorization": f"Bearer {made.json()['key']}"}
    listed = client.get("/cds-services", headers=key)
    assert listed.status_code == 200
    assert "mendari.referral-router" in {service["id"] for service in listed.json()["services"]}
    called = client.post(HOOK, json=order_sign(1), headers=key)
    assert called.status_code == 200, called.text
    assert len(called.json()["cards"]) == 1
    assert decisions_in(db) == 1


@pytest.mark.parametrize("alg", ["RS384", "ES384"])
def test_an_ehrs_signed_token_is_refused_as_a_credential_mendari_did_not_issue(
    served: tuple[TestClient, str], alg: str
) -> None:
    """CT-2b. An EHR's signed CDS Hooks token, RS384 (longer than any credential Mendari issues)
    or ES384 (shorter), is a `401` on discovery and on the hook. It is never read as an identity:
    a person's valid session riding along is not looked at, `/api/auth/me` sees no one, and
    nothing is decided."""
    client, db = served
    token = ehr_token(alg)
    assert token.startswith("eyJ") and token.count(".") == 2
    assert (len(token) > 512) is (alg == "RS384")  # past the bearer cap, or a prefix unknown
    session = person(db)
    assert client.get("/cds-services", headers=session).status_code == 200  # alone, it would do
    sent = {**session, "authorization": f"Bearer {token}"}
    for refused in (
        client.get("/cds-services", headers=sent),
        client.post(HOOK, json=order_sign(1), headers=sent),
    ):
        assert refused.status_code == 401
        assert refused.headers["www-authenticate"] == INVALID
        assert without_request_id(refused.json(), refused) == {
            "error": "AuthError",
            "detail": "the credential is not valid",
        }
        assert token not in refused.text
    me = client.get("/api/auth/me", headers={"authorization": f"Bearer {token}"}).json()
    assert me == {"mode": "sign-in", "signed_in": False}
    assert decisions_in(db) == 0


# -- the outside tools CI runs ------------------------------------------------------------------


def _jobs() -> dict[str, Any]:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    on = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as true
    assert {"push", "pull_request"} <= set(on)  # every pull request, and main
    jobs: dict[str, Any] = workflow["jobs"]
    return jobs


def _runs(job: dict[str, Any]) -> str:
    return "\n".join(" ".join(str(step.get("run", "")).split()) for step in job["steps"])


def _script(path: str) -> ModuleType:
    name = f"{Path(path).stem}_for_the_baseline"
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_ci_runs_the_hl7_validator_offline_over_mendaris_outputs_and_the_sandboxs_inputs() -> None:
    """CE-2a. The `fhir-conformance` job always runs, and nothing in it may fail quietly: the
    validator is downloaded at a pinned version and checksum, runs without a terminology server
    (`-tx n/a`) against US Core 6.1.0, over what Mendari writes (its audit export, Tasks and
    decision outputs) and the sandbox's inputs, and over the same files the in-process US Core
    check passes."""
    job = _jobs()["fhir-conformance"]
    assert "if" not in job
    assert not [s for s in job["steps"] if "if" in s or "continue-on-error" in s]
    assert set(job["env"]) == {"VALIDATOR_VERSION", "VALIDATOR_SHA256"}
    runs = _runs(job)
    assert "releases/download/${VALIDATOR_VERSION}/validator_cli.jar" in runs
    assert 'echo "${VALIDATOR_SHA256} var/validator/validator_cli.jar" | sha256sum -c -' in runs
    for writer in ("audit_fhir", "task_fhir", "sandbox_fhir", "fhir_output"):
        assert f"uv run python scripts/{writer}_sample.py var/fhir-samples" in runs, writer
    valid = "tests/fixtures/us-core/*.json var/fhir-samples/*.json var/fhir-terminology/*.json"
    validated = f"uv run python scripts/fhir_validate.py --ig var/fhir-terminology --valid {valid}"
    assert f"{validated} --invalid tests/fixtures/us-core/invalid/*.json" in runs
    arguments = _script("scripts/fhir_validate.py").ARGS
    assert arguments[arguments.index("-tx") + 1] == "n/a"  # offline: no terminology server
    assert arguments[arguments.index("-ig") + 1] == "hl7.fhir.us.core#6.1.0"
    from tests.test_us_core import test_conforming_bundles_have_no_issues as in_process

    marks: list[pytest.Mark] = getattr(in_process, "pytestmark", [])
    (cases,) = [mark.args[1] for mark in marks if mark.name == "parametrize"]
    assert cases
    for name in cases:
        path = f"tests/fixtures/us-core/{name}"
        assert (ROOT / path).is_file() and fnmatch.fnmatch(path, "tests/fixtures/us-core/*.json")


def test_ci_runs_the_smart_app_against_smart_launcher_v2() -> None:
    """CE-2b. The `sandbox-e2e` job always runs and only its Docker Hub sign-in may fail: it
    starts smart-launcher-v2 (pinned by digest) in front of HAPI, and runs the scripted SMART
    launch through it for the inbox and referral packs, with sign-in off and on, with `--strict`
    so a decision on input that does not conform fails the job."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["launcher"]["image"].startswith(
        "smartonfhir/smart-launcher-2@sha256:"
    )
    job = _jobs()["sandbox-e2e"]
    assert "if" not in job
    checked = [
        step
        for step in job["steps"]
        if not step.get("uses", "").startswith("docker/login-action@")
        and step.get("if") != "failure()"  # the logs, after a failure
    ]
    assert len(checked) == len(job["steps"]) - 2
    assert not [s for s in checked if "if" in s or "continue-on-error" in s]
    runs = _runs(job)
    up = "docker compose --profile signin up -d --build --wait engine launcher engine-signin"
    assert up in runs
    for engine in ("engine", "engine-signin"):
        for pack in ("mendari/referral-router", "mendari/patient-inbox-router"):
            launch = (
                f"docker compose exec -T {engine} mendari sandbox smart-launch --strict "
                f"--engine http://{engine}:8000 --launcher http://launcher "
                f"--fhir http://fhir:8080/fhir --pack {pack}"
            )
            assert launch in runs, (engine, pack)
    assert "mendari smart check http://launcher/v/r4/fhir" in runs
