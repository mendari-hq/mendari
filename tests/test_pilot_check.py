# SPDX-License-Identifier: Apache-2.0
"""The scripted v0.6 pilot (sandbox/pilot_check.py). The pilot itself runs against a real OpenID
Connect provider in the CI job `pilot-check`."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from mendari.cli import main
from mendari.errors import MendariError
from mendari.sandbox import DEMO_AGENT, pilot_check
from tests.conftest import CLINICAL_DIR, DEMO_AGENT_DIR, TRIAGE_DIR

ENGINE = "http://engine.test"
# A risk level 2 agent of the test fixtures, named by its folder as CI names it.
CLINICAL = str(CLINICAL_DIR)


@pytest.fixture(autouse=True)
def _the_demo_agent_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pilot runs the bundled demo agent unless told otherwise: here it is on the search
    path, as in a release (the test suite's bundled agents are the fixtures)."""
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(DEMO_AGENT_DIR.parent))


def test_the_pilot_needs_sign_in_and_phi_encryption() -> None:
    health = {"status": "ok", "sign_in": False, "phi_mode": "off"}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=health))
    with pytest.raises(MendariError, match="sign-in on and PHI mode encrypt"):
        pilot_check.run(engine=ENGINE, transport=transport)


def test_the_pilot_reads_the_phi_mode_once_the_admin_has_signed_in() -> None:
    """Anonymous health is only status, version and sign-in; the PHI mode is a setting, shown
    to a signed-in reader. Its reads with the session cookie say they are Mendari's own
    (`X-Mendari-Request`): without Fetch Metadata, the engine refuses them otherwise."""
    asked: list[tuple[bool, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path, signed_in = request.url.path, "cookie" in request.headers
        if path == "/api/health":
            asked.append((signed_in, request.headers.get("x-mendari-request")))
            public = {"status": "ok", "version": "1", "sign_in": True}
            return httpx.Response(200, json={**public, "phi_mode": "off"} if signed_in else public)
        if path == "/auth/login":
            started = {
                "location": "http://idp.test/authorize?state=s",
                "set-cookie": "__Host-mendari_signin=b; Secure; Path=/",
            }
            return httpx.Response(302, headers=started)
        if path == "/authorize":
            return httpx.Response(302, headers={"location": f"{ENGINE}/auth/callback?code=c"})
        if path == "/auth/callback":
            assert request.headers["cookie"] == "__Host-mendari_signin=b"  # carried by hand
            done = {"location": "/", "set-cookie": "__Host-mendari_session=s; Secure; Path=/"}
            return httpx.Response(302, headers=done)
        return httpx.Response(200, json={"principal": {"roles": ["admin"]}})

    with pytest.raises(MendariError, match="sign-in on and PHI mode encrypt"):
        pilot_check.run(engine=ENGINE, transport=httpx.MockTransport(handler))
    assert asked == [(False, None), (True, "1")]


def test_a_sign_in_must_come_back_to_the_engine() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            started = {
                "location": "http://idp.test/authorize?state=s",
                "set-cookie": "mendari_signin=b; Secure; Path=/",
            }
            return httpx.Response(302, headers=started)
        return httpx.Response(302, headers={"location": "http://elsewhere.test/cb?code=c"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        pytest.raises(MendariError, match=r"elsewhere\.test"),
    ):
        pilot_check.Pilot(ENGINE, http).sign_in("reviewer")


def test_a_step_that_answers_the_wrong_status_stops_the_pilot() -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as http:
        pilot = pilot_check.Pilot(ENGINE, http)
        with pytest.raises(MendariError, match="the viewer may not resolve a review: HTTP 200"):
            pilot.expect(http.get(ENGINE), 403, "the viewer may not resolve a review")


def test_the_cli_reports_the_steps_or_the_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result: dict[str, Any] = {"steps": ["health"], "decided": 1}
    asked: list[dict[str, Any]] = []

    def run(**kwargs: Any) -> dict[str, Any]:
        asked.append(kwargs)
        return result

    monkeypatch.setattr(pilot_check, "run", run)
    assert main(["sandbox", "pilot-check", "--engine", ENGINE]) == 0
    assert '"decided": 1' in capsys.readouterr().out
    # The bundled demo agent, and no approval, unless the command names them.
    assert asked == [{"engine": ENGINE, "messages": 12, "agent": DEMO_AGENT, "approve": None}]
    named = ["--agent", str(TRIAGE_DIR), "--approve", CLINICAL, "--messages", "3"]
    assert main(["sandbox", "pilot-check", "--engine", ENGINE, *named]) == 0
    assert asked[1] == {
        "engine": ENGINE,
        "messages": 3,
        "agent": str(TRIAGE_DIR),
        "approve": CLINICAL,
    }
    capsys.readouterr()

    def fail(**kwargs: Any) -> dict[str, Any]:
        raise MendariError("the viewer could read an input")

    monkeypatch.setattr(pilot_check, "run", fail)
    assert main(["sandbox", "pilot-check", "--engine", ENGINE]) == 1
    assert "pilot-check: the viewer could read an input" in capsys.readouterr().err
