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
from mendari.sandbox import fhir_workflow, smart_launch
from mendari.sandbox.fhir_records import REFERRAL, SANDBOX_TAG
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


def test_a_referral_patient_is_found_beside_the_prior_authorization_packs_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prior-authorization pack's seeded orders are tagged ServiceRequests too, and come
    first here: the referral router's launch still finds a patient with a referral (the search
    names the referral category), and the inbox's search is as before."""
    orders = [
        e["resource"]
        for pack in (fhir_workflow.PRIOR_AUTH, fhir_workflow.REFERRAL)
        for e in fhir_workflow.seed_bundle(pack, 2)["entry"]
        if e["resource"]["resourceType"] == "ServiceRequest"
    ]
    assert not orders[0].get("category")  # a prior-authorization order: not a referral
    tag = f"{SANDBOX_TAG['system']}|{SANDBOX_TAG['code']}"
    asked: list[dict[str, str]] = []

    def get(url: str, *, params: dict[str, str], **_: Any) -> httpx.Response:
        asked.append(params)
        found = [
            o
            for o in orders
            if url.endswith(f"/{o['resourceType']}")
            and params["_tag"] == tag
            and all(
                _coded(o.get("category"), params[name]) for name in params if name == "category"
            )
        ]
        entries = [{"resource": o} for o in found[: int(params["_count"])]]
        return httpx.Response(200, json={"entry": entries}, request=httpx.Request("GET", url))

    monkeypatch.setattr(smart_launch.httpx, "get", get)
    found = smart_launch.find_patient("http://fhir:8080/fhir", "mendari/referral-router")
    referrals = [o for o in orders if o.get("category")]
    assert found == referrals[0]["subject"]["reference"].rsplit("/", 1)[-1]
    assert asked[0]["category"] == f"{REFERRAL['system']}|{REFERRAL['code']}"
    with pytest.raises(MendariError):  # no Communication here
        smart_launch.find_patient("http://fhir:8080/fhir", "mendari/patient-inbox-router")
    assert "category" not in asked[1]


def _coded(categories: Any, wanted: str) -> bool:
    """Whether a `category` holds the `system|code` a token search asks for."""
    system, _, code = wanted.partition("|")
    return any(
        coding.get("system") == system and coding.get("code") == code
        for concept in categories or []
        for coding in concept.get("coding", [])
    )


def test_the_launch_reads_its_session_as_the_dashboard_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the callback, every call sends the launch cookie and Mendari's own request header:
    the script sends no Fetch Metadata, and without the header its reads would be refused as
    another site's link (server/origin.py)."""
    engine, launcher = "http://engine.test", "http://launcher.test"
    sent: list[tuple[str, str, dict[str, str]]] = []
    session = {"active": True, "patient": "1", "fhir_user": "Practitioner/1"}
    decision = {"id": "d-1", "outcome": "AUTO", "recommendation": "routine", "input_issues": []}

    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        sent.append((request.method, path, dict(request.headers)))
        if path == "/smart/launch":
            started = {"location": f"{launcher}/authorize", "set-cookie": "mendari_launch=b"}
            return httpx.Response(302, headers=started)
        if path == "/authorize":
            return httpx.Response(302, headers={"location": f"{engine}/smart/callback?code=c"})
        if path == "/smart/callback":
            return httpx.Response(302, headers={"location": "/", "set-cookie": "mendari_smart=s"})
        if path == "/api/smart/session":
            return httpx.Response(200, json=session if len(sent) < 6 else {"active": False})
        if path == "/api/smart/focus":
            return httpx.Response(200, json=[{"ref": "ServiceRequest/2"}])
        if path == "/api/smart/decide":
            return httpx.Response(200, json={"decision": decision})
        return httpx.Response(204)  # the logout

    real = httpx.Client
    monkeypatch.setattr(
        smart_launch.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(answer), **kw)
    )
    done = smart_launch.run(
        engine=engine, launcher=launcher, fhir="", pack_id="mendari/referral-router", patient="1"
    )
    assert done["session_ended"] and done["decided"][0]["decision_id"] == "d-1"
    calls = [(method, path, headers) for method, path, headers in sent if path.startswith("/api/")]
    assert [path for _, path, _ in calls] == [
        "/api/smart/session", "/api/smart/focus", "/api/smart/decide", "/api/smart/logout",
        "/api/smart/session",
    ]  # fmt: skip
    for _, _, headers in calls:
        assert (headers["cookie"], headers["x-mendari-request"]) == ("mendari_smart=s", "1")


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
