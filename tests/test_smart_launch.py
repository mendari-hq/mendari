"""The scripted sandbox launch (sandbox/smart_launch.py). The launch itself runs against the real
compose stack in the CI job `sandbox-e2e`."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from openhealth.cli import main
from openhealth.errors import OpenHealthError
from openhealth.sandbox import smart_launch


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
    assert smart_launch.find_patient("http://fhir:8080/fhir/", "openhealth/referral-router") == "42"
    assert seen[0]["url"] == "http://fhir:8080/fhir/ServiceRequest"
    assert seen[0]["headers"]["cache-control"] == "no-cache"  # HAPI caches searches
    with pytest.raises(OpenHealthError, match="launches"):
        smart_launch.find_patient("http://fhir:8080/fhir", "openhealth/ai-guard")


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
