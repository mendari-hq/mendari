# SPDX-License-Identifier: Apache-2.0
"""The API reference (/docs/reference) in a real browser: Swagger UI from vendored files under
the page's strict CSP, with no request to another origin, and "Try it out" working.

Skipped by the default `pytest` run; run with `uv run pytest -m browser`.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

# Records every CSP violation before any of the page's own scripts run.
WATCH_CSP = """window.__violations = [];
document.addEventListener("securitypolicyviolation", (event) => window.__violations.push(
  `${event.violatedDirective} ${event.blockedURI} ${event.sourceFile}:${event.lineNumber}`));"""


def test_try_it_out_works_under_the_csp_and_nothing_leaves_the_origin(
    page: Page, live_server: str
) -> None:
    requests: list[str] = []
    errors: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(WATCH_CSP)

    page.goto("/redoc")  # ReDoc's old address: a 308 to the reference
    assert page.url == f"{live_server}/docs/reference"
    expect(page.locator(".swagger-ui .info .title")).to_contain_text("Mendari")
    operation = page.locator(
        ".opblock-get", has=page.locator('.opblock-summary-path[data-path="/api/agents"]')
    )
    operation.locator(".opblock-summary-control").click()
    operation.get_by_role("button", name="Try it out").click()
    operation.get_by_role("button", name="Execute").click()
    response = operation.locator(".live-responses-table .response")
    expect(response.locator(".response-col_status")).to_have_text("200")
    expect(response.locator(".response-col_description")).to_contain_text(
        "mendari/patient-inbox-router"
    )

    assert page.evaluate("window.__violations") == []
    assert [url for url in requests if not url.startswith(f"{live_server}/")] == []
    assert f"{live_server}/api/agents" in requests  # Try it out called this origin
    assert not [url for url in requests if "validator" in url]
    assert errors == []


def test_the_reference_follows_the_dashboards_theme(page: Page) -> None:
    page.emulate_media(color_scheme="dark")
    page.goto("/docs/reference")
    expect(page.locator("html")).to_have_class("dark-mode")  # "System", the default
    page.evaluate("localStorage.setItem('oh.theme', 'light')")
    page.reload()
    expect(page.locator(".swagger-ui .info")).to_be_visible()
    expect(page.locator("html")).not_to_have_class("dark-mode")
