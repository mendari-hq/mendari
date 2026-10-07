# SPDX-License-Identifier: Apache-2.0
"""The scripted sandbox launch (sandbox/smart_launch.py). The launch itself runs against the real
compose stack in the CI job `sandbox-e2e`."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from mendari.cli import main
from mendari.errors import MendariError
from mendari.sandbox import smart_launch
from tests.test_smart import (
    DECIDE_BODY,
    REGISTERED,
    TENANT_A,
    TENANT_B,
    full_launch,
    signed_site,
    tenant,
)


def test_the_launch_value_is_what_the_launcher_decodes() -> None:
    """smart-launcher-v2's codec: a base64url JSON array (launch type, patient, provider,
    encounter, skip login, skip auth, ..., client type, PKCE)."""
    value = smart_launch.launch_value("p-1", "pr-1")
    fields = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    assert fields[:6] == [0, "p-1", "pr-1", "NONE", 1, 1]  # provider-ehr, skip login and consent
    assert fields[14:16] == [0, 1]  # a public client; PKCE when the client sends a challenge
    assert "=" not in value


def test_a_patient_with_a_work_item_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def get(url: str, **kwargs: Any) -> httpx.Response:
        seen.append({"url": url, **kwargs})
        entry = {
            "resource": {"resourceType": "ServiceRequest", "subject": {"reference": "Patient/42"}}
        }
        return httpx.Response(200, json={"entry": [entry]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(smart_launch.httpx, "get", get)
    assert smart_launch.find_patient("http://fhir:8080/fhir/", "mendari/referral-router") == "42"
    assert seen[0]["url"] == "http://fhir:8080/fhir/ServiceRequest"
    assert seen[0]["headers"]["cache-control"] == "no-cache"  # HAPI caches searches
    with pytest.raises(MendariError, match="launches"):
        smart_launch.find_patient("http://fhir:8080/fhir", "mendari/ai-guard")


def test_the_cli_fails_unless_something_was_decided_and_the_session_ended(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result: dict[str, Any] = {
        "patient": "1",
        "fhir_user": None,
        "work_items": 1,
        "session_ended": True,
        "decided": [{"focus": "ServiceRequest/2", "input_issues": 1}],
    }
    monkeypatch.setattr(smart_launch, "run", lambda **kwargs: result)
    args = ["sandbox", "smart-launch", "--engine", "e", "--launcher", "l", "--fhir", "f"]
    assert main(args) == 0
    assert main([*args, "--strict"]) == 1  # a US Core issue
    assert "did not conform to US Core" in capsys.readouterr().err
    result["decided"] = []
    assert main(args) == 1


@pytest.mark.parametrize("db_target", ["sqlite", "postgres"], indirect=True)
def test_who_asked_reads_the_chain_as_the_expected_organization(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--expect-org` reads the engine's own store as that organization: a launch's decision
    names the EHR's issuer and subject there, and nobody as any other organization. Both
    backends."""
    a, b = tenant(TENANT_A), tenant(TENANT_B)
    app = signed_site(db_target, monkeypatch, REGISTERED, a, b)
    assert full_launch(app, a).status_code == 302
    decided = app.post("/api/smart/decide", json=DECIDE_BODY)
    assert decided.status_code == 200, decided.text
    decision = decided.json()["decision"]["id"]
    assert smart_launch.who_asked(db_target, "clinic-a", [decision]) == [f"{TENANT_A}|clinician-1"]
    for other in ("clinic-b", "default"):
        assert smart_launch.who_asked(db_target, other, [decision]) == [None]


@pytest.mark.parametrize(
    ("actor", "named"),
    [
        ("http://launcher/v/r4/fhir|b4d27c82", True),
        ("http://launcher|sub-1", True),
        ("clinician@pilot.example.org", False),  # an email: not what the launcher sends
        ("http://launcher/v/r4/fhir|", False),
        ("http://launcher.evil/v/r4/fhir|sub", False),
        ("http://other/v/r4/fhir|sub", False),
        ("open mode", False),
        (None, False),
    ],
)
def test_the_launchers_user_is_named_by_issuer_and_subject(actor: str | None, named: bool) -> None:
    assert smart_launch.names_launcher_user(actor, "http://launcher/") is named


def test_the_cli_fails_unless_each_decision_is_the_organizations_and_names_its_user(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}
    result: dict[str, Any] = {
        "patient": "1",
        "fhir_user": "Practitioner/sandbox-clinician",
        "work_items": 2,
        "session_ended": True,
        "decided": [
            {"focus": "ServiceRequest/2", "decision_id": "d-1", "input_issues": 0},
            {"focus": "ServiceRequest/3", "decision_id": "d-2", "input_issues": 0},
        ],
        "org": "pilot",
        "requested_by": ["http://launcher/v/r4/fhir|abc", "http://launcher/v/r4/fhir|abc"],
    }

    def run(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return result

    monkeypatch.setattr(smart_launch, "run", run)
    args = ["sandbox", "smart-launch", "--engine", "e", "--launcher", "http://launcher"]
    args += ["--fhir", "f", "--expect-org", "pilot", "--db", "/data/mendari.db"]
    assert main(args) == 0
    assert (seen["expect_org"], seen["db"]) == ("pilot", "/data/mendari.db")
    result["requested_by"] = [None, "key k-1"]
    assert main(args) == 1
    err = capsys.readouterr().err
    assert "d-1 is not pilot's or names nobody" in err
    assert "d-2 names 'key k-1', not <issuer>|<subject>" in err


def test_expecting_an_organization_needs_the_engines_database() -> None:
    """Refused before anything is sent: the check would have nothing to read."""
    with pytest.raises(MendariError, match="database"):
        smart_launch.run(
            engine="http://e", launcher="http://l", fhir="http://f", pack_id="x", expect_org="o"
        )
