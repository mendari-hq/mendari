# SPDX-License-Identifier: Apache-2.0
"""Two EHR launches in one browser, through the real server (sign-in on, an EHR registered to
the one organization, no one signed in) and the fake EHR of tests/test_smart.py, serving two of
the chart fixture's synthetic patients: run with `uv run pytest -m browser`."""

from __future__ import annotations

import copy
import functools
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Route, expect

import tests.test_smart as fake
from mendari.server import smart_routes
from mendari.server.app import create_app
from mendari.standards.fhir_client import FhirClient, GuardedHttp
from tests.browser.conftest import CHART, serving
from tests.conftest import encrypt_mode
from tests.support.fixture_records import SHOWCASE

pytestmark = pytest.mark.browser

# The chart fixture's showcase patient and a second one, by their ids and names.
FIRST_ID, SECOND_ID = SHOWCASE.id, "chart-p2"
FIRST, SECOND = f"Case {SHOWCASE.id} Synthetic", "Second Synthetic"


def two_patients() -> fake.FakeEhr:
    """The fake EHR, serving the chart fixture's showcase record (its patient is the work item
    the fixture decides about) and a second synthetic patient."""
    ehr = fake.FakeEhr()
    record = [entry["resource"] for entry in SHOWCASE.bundle()["entry"]]
    patient = next(r for r in record if r["resourceType"] == "Patient")
    other = copy.deepcopy(patient) | {"id": SECOND_ID}
    other["name"] = [{"given": ["Second"], "family": "Synthetic"}]
    ehr.resources = [*record, other]
    return ehr


@contextmanager
def authorizing(ehr: fake.FakeEhr, back: list[str]) -> Iterator[str]:
    """The EHR's authorization endpoint as a page on 127.0.0.1 (a browser follows the launch's
    redirect there, where a route cannot answer): it approves every launch and sends the
    browser back to Mendari's callback (`back[0]`). Yields the endpoint's origin."""

    class Approve(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # the standard library names it
            code = ehr.authorize(self.path)
            state = parse_qs(urlsplit(self.path).query)["state"][0]
            self.send_response(302)
            query = urlencode({"code": code, "state": state})
            self.send_header("location", f"{back[0]}/smart/callback?{query}")
            self.end_headers()

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Approve)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://localhost:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, fake.FakeEhr]]:
    """Mendari at `localhost` with sign-in on and the fake EHR registered to `default`; the
    base URL and the EHR."""
    ehr = two_patients()
    back: list[str] = []
    with authorizing(ehr, back) as authorize:
        ehr.config_overrides = {"authorization_endpoint": f"{authorize}/auth/authorize"}
        monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")  # never reached
        monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
        encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
        monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", fake.CLIENT)
        monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", f"{fake.EHR},{authorize}")
        monkeypatch.setenv("MENDARI_SMART_EHRS", f"{fake.ISS}=default:decide")
        transport = httpx.MockTransport(ehr)
        monkeypatch.setattr(
            smart_routes, "GuardedHttp", functools.partial(GuardedHttp, transport=transport)
        )
        monkeypatch.setattr(
            smart_routes, "FhirClient", functools.partial(FhirClient, transport=transport)
        )
        with serving(create_app(db_path=tmp_path / "a.db")) as port:
            base = f"http://localhost:{port}"
            back.append(base)
            monkeypatch.setattr(fake, "APP", base)  # the redirect URI the fake EHR expects
            yield base, ehr


def launch(context: BrowserContext, base: str, ehr: fake.FakeEhr, patient: str) -> Page:
    """The EHR opens Mendari in a new window, on `patient`."""
    ehr.patient = patient
    page = context.new_page()
    query = urlencode({"iss": fake.ISS, "launch": "xyz", "agent": CHART})
    page.goto(f"{base}/smart/launch?{query}")
    return page


def test_a_launch_that_replaces_the_window_s_mid_render_is_never_shown(
    browser: Browser, site: tuple[str, fake.FakeEhr]
) -> None:
    """Window A checked its launch (still its own), then, before it read the patient and the
    items, a second launch in the same browser, for another patient, replaced it. The server
    refuses A's reads (they name A's launch), so A says it was replaced and never shows the
    other patient, while the second window shows its own."""
    base, ehr = site
    context = browser.new_context()
    a = launch(context, base, ehr, FIRST_ID)
    expect(a.locator(".patient-name")).to_have_text(FIRST)
    a.evaluate(
        """window.seen = [];
        new MutationObserver(() => document.querySelectorAll(".patient-name")
          .forEach((n) => window.seen.push(n.textContent)))
          .observe(document.querySelector("#view"), { childList: true, subtree: true });"""
    )
    held = {"armed": True}
    windows: list[Page] = []

    def race(route: Route) -> None:
        if not held["armed"]:
            route.continue_()
            return
        held["armed"] = False
        answer = route.fetch()  # A's launch, still the browser's
        b = launch(context, base, ehr, SECOND_ID)  # replaces it before A reads the patient
        expect(b.locator(".patient-name")).to_have_text(SECOND)
        windows.append(b)
        route.fulfill(response=answer)

    a.route("**/api/smart/session", race)
    a.evaluate("document.querySelector('#agent-select').dispatchEvent(new Event('change'))")
    replaced = a.locator(".empty-state h2")
    expect(replaced).to_have_text("This window's launch was replaced")
    expect(a.locator(".patient-name")).to_have_count(0)
    assert SECOND not in a.evaluate("window.seen")
    assert windows and windows[0].locator(".patient-name").inner_text() == SECOND
    context.close()
